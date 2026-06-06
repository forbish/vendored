#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Protocol, cast

__version__ = "1.0.0"
MANIFEST_PATH = Path("vendored.json")
EXECUTABLE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
DEFAULT_REF_TEMPLATE = "{{version}}"
MAX_FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0


class SyncError(Exception):
    pass


class DriftError(SyncError):
    pass


class SelectionError(SyncError):
    pass


class ReadableResponse(Protocol):
    def __enter__(self) -> ReadableResponse: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...

    def read(self, size: int = -1) -> bytes: ...


UrlOpen = Callable[[str], ReadableResponse]

# Hosts we will attach a GitHub token to. The token is NOT sent to redirect
# targets (e.g. release-asset downloads redirect to objects.githubusercontent.com,
# a presigned URL that needs no auth), avoiding credential leakage cross-host.
GITHUB_AUTH_HOSTS = frozenset(
    {
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "codeload.github.com",
    }
)


def github_token() -> str | None:
    for name in ("GITHUB_TOKEN", "RENOVATE_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token
    return None


def build_request(url: str, token: str | None = None) -> urllib.request.Request:
    request = urllib.request.Request(url)
    if token is None:
        token = github_token()
    if token and urllib.parse.urlsplit(url).hostname in GITHUB_AUTH_HOSTS:
        request.add_header("Authorization", f"Bearer {token}")
    return request


class _AuthStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop the Authorization header when a redirect crosses to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            new_host = urllib.parse.urlsplit(newurl).hostname
            if old_host != new_host:
                new_req.headers.pop("Authorization", None)
                new_req.unredirected_hdrs.pop("Authorization", None)
        return new_req


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_AuthStrippingRedirectHandler())


def default_urlopen(url: str) -> ReadableResponse:
    return cast(ReadableResponse, _build_opener().open(build_request(url), timeout=30))


@dataclass(frozen=True)
class VendorFile:
    target: Path
    source: str | None = None
    url: str | None = None
    digest: str | None = None
    executable: bool = False


@dataclass(frozen=True)
class FetchConfig:
    type: str
    repo: str | None = None
    ref_template: str = DEFAULT_REF_TEMPLATE
    asset: str | None = None

    def ref_for_version(self, version: str) -> str:
        return self.ref_template.replace("{{version}}", version)


@dataclass(frozen=True)
class Vendor:
    id: str
    fetch: FetchConfig
    files: tuple[VendorFile, ...]
    datasource: str | None = None
    dep_name: str | None = None
    package_name: str | None = None
    versioning: str | None = None
    current_value: str | None = None
    current_digest: str | None = None
    update_group: str | None = None
    extract_version: str | None = None


@dataclass(frozen=True)
class SyncPlanItem:
    url: str
    target: Path
    executable: bool = False
    digest: str | None = None


@dataclass(frozen=True)
class Manifest:
    vendors: tuple[Vendor, ...]

    def vendor(self, vendor_id: str) -> Vendor:
        for vendor in self.vendors:
            if vendor.id == vendor_id:
                return vendor
        raise SelectionError(f"unknown vendor id: {vendor_id}")

    def _matches(self, vendor: Vendor, selector: str) -> bool:
        return selector in (vendor.id, vendor.dep_name, vendor.package_name)

    def select(
        self, vendor_ids: list[str] | None = None, fetch_types: list[str] | None = None
    ) -> tuple[Vendor, ...]:
        vendors = self.vendors
        if vendor_ids:
            selected: list[Vendor] = []
            seen: set[str] = set()
            for selector in vendor_ids:
                matches = [v for v in self.vendors if self._matches(v, selector)]
                if not matches:
                    raise SelectionError(f"unknown vendor id: {selector}")
                for vendor in matches:
                    if vendor.id not in seen:
                        seen.add(vendor.id)
                        selected.append(vendor)
            vendors = tuple(selected)
        if fetch_types:
            filtered = tuple(v for v in vendors if v.fetch.type in fetch_types)
            if vendor_ids:
                surviving = {v.id for v in filtered}
                for selector in vendor_ids:
                    matched_ids = {
                        v.id for v in self.vendors if self._matches(v, selector)
                    }
                    if not (matched_ids & surviving):
                        raise SelectionError(
                            f"vendor {selector!r} exists but does not match fetch type(s) {fetch_types!r}"
                        )
            vendors = filtered
        return vendors


def require_string(payload: object, field: str) -> str:
    if not isinstance(payload, str) or not payload:
        raise ValueError(f"{field} must be a non-empty string")
    return payload


def optional_string(payload: object, field: str) -> str | None:
    if payload is None:
        return None
    return require_string(payload, field)


def require_object(payload: object, field: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError(f"{field} must be an object")
    return payload


def require_list(payload: object, field: str) -> list[object]:
    if not isinstance(payload, list):
        raise ValueError(f"{field} must be an array")
    return payload


def require_relative_target(raw_target: object, field: str) -> Path:
    target = Path(require_string(raw_target, field))
    if target.is_absolute() or ".." in target.parts:
        raise ValueError(f"{field} must be a relative path inside the repository")
    return target


def require_relative_source(raw_source: object, field: str) -> str:
    source = require_string(raw_source, field)
    source_path = PurePosixPath(source)
    if source_path.is_absolute() or ".." in source_path.parts:
        raise ValueError(f"{field} must be a relative upstream path")
    return source


def github_raw_url(repo: str, ref: str, source: str) -> str:
    quoted_source = "/".join(
        urllib.parse.quote(part, safe="") for part in PurePosixPath(source).parts
    )
    quoted_ref = urllib.parse.quote(ref, safe="/")
    return f"https://raw.githubusercontent.com/{repo}/{quoted_ref}/{quoted_source}"


def parse_fetch_config(raw_vendor: dict[str, object], index: int) -> FetchConfig:
    raw_fetch = require_object(raw_vendor.get("fetch"), f"vendors[{index}].fetch")
    fetch_type = require_string(raw_fetch.get("type"), f"vendors[{index}].fetch.type")

    if fetch_type == "github-tagged-files":
        repo = require_string(raw_fetch.get("repo"), f"vendors[{index}].fetch.repo")
        ref_template = raw_fetch.get("refTemplate", DEFAULT_REF_TEMPLATE)
        return FetchConfig(
            type=fetch_type,
            repo=repo,
            ref_template=require_string(
                ref_template, f"vendors[{index}].fetch.refTemplate"
            ),
        )
    if fetch_type == "github-ref-files":
        repo = require_string(raw_fetch.get("repo"), f"vendors[{index}].fetch.repo")
        ref_template = raw_fetch.get("refTemplate", DEFAULT_REF_TEMPLATE)
        return FetchConfig(
            type=fetch_type,
            repo=repo,
            ref_template=require_string(
                ref_template, f"vendors[{index}].fetch.refTemplate"
            ),
        )
    if fetch_type == "github-release-asset":
        repo = require_string(raw_fetch.get("repo"), f"vendors[{index}].fetch.repo")
        return FetchConfig(
            type=fetch_type,
            repo=repo,
            asset=require_string(
                raw_fetch.get("asset"), f"vendors[{index}].fetch.asset"
            ),
        )
    if fetch_type == "url-files":
        return FetchConfig(type=fetch_type)
    raise ValueError(f"vendors[{index}].fetch.type is unsupported: {fetch_type}")


def parse_vendor_files(
    raw_vendor: dict[str, object], index: int, fetch: FetchConfig
) -> tuple[VendorFile, ...]:
    raw_files = require_list(raw_vendor.get("files"), f"vendors[{index}].files")
    if not raw_files:
        raise ValueError(f"vendors[{index}].files must not be empty")

    files: list[VendorFile] = []
    for file_index, raw_file in enumerate(raw_files):
        file_object = require_object(raw_file, f"vendors[{index}].files[{file_index}]")
        target = require_relative_target(
            file_object.get("target"),
            f"vendors[{index}].files[{file_index}].target",
        )
        executable = file_object.get("executable", False)
        if not isinstance(executable, bool):
            raise ValueError(
                f"vendors[{index}].files[{file_index}].executable must be a boolean"
            )

        source = file_object.get("source")
        if fetch.type in {"github-tagged-files", "github-ref-files"}:
            source = require_relative_source(
                source, f"vendors[{index}].files[{file_index}].source"
            )
        elif source is not None and not isinstance(source, str):
            raise ValueError(
                f"vendors[{index}].files[{file_index}].source must be a string when provided"
            )

        file_url: str | None = None
        file_digest: str | None = None
        if fetch.type == "url-files":
            file_url = require_string(
                file_object.get("url"),
                f"vendors[{index}].files[{file_index}].url",
            )
            file_digest = require_string(
                file_object.get("digest"),
                f"vendors[{index}].files[{file_index}].digest",
            )
        else:
            file_digest = optional_string(
                file_object.get("digest"),
                f"vendors[{index}].files[{file_index}].digest",
            )

        files.append(
            VendorFile(
                target=target,
                source=source,
                url=file_url,
                digest=file_digest,
                executable=executable,
            )
        )
    return tuple(files)


def load_manifest(path: Path) -> Manifest:
    try:
        text = path.read_text()
    except FileNotFoundError as exc:
        raise SyncError(f"manifest not found: {path}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SyncError(f"invalid JSON in {path}: {exc}") from exc
    root = require_object(payload, "manifest")
    raw_vendors = require_list(root.get("vendors"), "vendors")

    vendors: list[Vendor] = []
    seen_ids: set[str] = set()
    for index, raw_vendor in enumerate(raw_vendors):
        vendor_object = require_object(raw_vendor, f"vendors[{index}]")
        vendor_id = require_string(vendor_object.get("id"), f"vendors[{index}].id")
        if vendor_id in seen_ids:
            raise ValueError(f"duplicate vendor id: {vendor_id}")
        seen_ids.add(vendor_id)

        fetch = parse_fetch_config(vendor_object, index)

        if fetch.type == "url-files":
            datasource = optional_string(
                vendor_object.get("datasource"), f"vendors[{index}].datasource"
            )
            dep_name = optional_string(
                vendor_object.get("depName"), f"vendors[{index}].depName"
            )
            versioning = optional_string(
                vendor_object.get("versioning"), f"vendors[{index}].versioning"
            )
            current_value = optional_string(
                vendor_object.get("currentValue"), f"vendors[{index}].currentValue"
            )
        else:
            datasource = require_string(
                vendor_object.get("datasource"), f"vendors[{index}].datasource"
            )
            dep_name = require_string(
                vendor_object.get("depName"), f"vendors[{index}].depName"
            )
            versioning = require_string(
                vendor_object.get("versioning"), f"vendors[{index}].versioning"
            )
            current_value = require_string(
                vendor_object.get("currentValue"), f"vendors[{index}].currentValue"
            )

        package_name = optional_string(
            vendor_object.get("packageName"), f"vendors[{index}].packageName"
        )
        current_digest = optional_string(
            vendor_object.get("currentDigest"), f"vendors[{index}].currentDigest"
        )
        update_group = optional_string(
            vendor_object.get("updateGroup"), f"vendors[{index}].updateGroup"
        )
        extract_version = optional_string(
            vendor_object.get("extractVersion"), f"vendors[{index}].extractVersion"
        )
        if fetch.type == "github-ref-files" and current_digest is None:
            raise ValueError(
                f"vendors[{index}].currentDigest is required for github-ref-files"
            )
        vendor = Vendor(
            id=vendor_id,
            datasource=datasource,
            dep_name=dep_name,
            package_name=package_name,
            versioning=versioning,
            current_value=current_value,
            current_digest=current_digest,
            fetch=fetch,
            files=parse_vendor_files(vendor_object, index, fetch),
            update_group=update_group,
            extract_version=extract_version,
        )
        vendors.append(vendor)

    return Manifest(vendors=tuple(vendors))


def plan_vendor_sync(vendor: Vendor) -> list[SyncPlanItem]:
    if vendor.fetch.type == "github-tagged-files":
        ref = vendor.fetch.ref_for_version(
            require_string(vendor.current_value, "currentValue")
        )
        return [
            SyncPlanItem(
                url=github_raw_url(
                    require_string(vendor.fetch.repo, "repo"),
                    ref,
                    require_string(vendor_file.source, "source"),
                ),
                target=vendor_file.target,
                executable=vendor_file.executable,
                digest=vendor_file.digest,
            )
            for vendor_file in vendor.files
        ]
    if vendor.fetch.type == "github-ref-files":
        ref = vendor.current_digest or vendor.fetch.ref_for_version(
            require_string(vendor.current_value, "currentValue")
        )
        return [
            SyncPlanItem(
                url=github_raw_url(
                    require_string(vendor.fetch.repo, "repo"),
                    ref,
                    require_string(vendor_file.source, "source"),
                ),
                target=vendor_file.target,
                executable=vendor_file.executable,
                digest=vendor_file.digest,
            )
            for vendor_file in vendor.files
        ]
    if vendor.fetch.type == "github-release-asset":
        url = (
            f"https://github.com/{vendor.fetch.repo}/releases/download/"
            f"{vendor.current_value}/{vendor.fetch.asset}"
        )
        return [
            SyncPlanItem(
                url=url,
                target=vendor_file.target,
                executable=vendor_file.executable,
                digest=vendor_file.digest,
            )
            for vendor_file in vendor.files
        ]
    raise ValueError(f"unsupported fetch type: {vendor.fetch.type}")


def resolve_target(repo_root: Path, relative_target: Path) -> Path:
    if relative_target.is_absolute():
        raise ValueError(f"target must be relative: {relative_target}")
    resolved_root = repo_root.resolve()
    resolved_target = (repo_root / relative_target).resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"target escapes repository: {relative_target}") from exc
    return resolved_target


def is_executable(path: Path) -> bool:
    return path.stat().st_mode & EXECUTABLE_BITS == EXECUTABLE_BITS


def apply_executable_mode(path: Path) -> None:
    current_mode = path.stat().st_mode
    path.chmod(current_mode | EXECUTABLE_BITS)


def ensure_target_exists(
    path: Path, display_path: Path, vendor_id: str, *, check: bool
) -> None:
    if path.exists():
        return
    if check:
        raise DriftError(
            f"{vendor_id}: drift detected, missing {display_path.as_posix()}"
        )
    raise SyncError(f"{vendor_id}: missing target {display_path.as_posix()}")


def sha256_digest_path(path: Path, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _fetch_to_temp(
    url: str, dest: Path, urlopen: UrlOpen, chunk_size: int = 65536
) -> tuple[Path, str]:
    tmp = dest.with_name(dest.name + ".sync-tmp")
    last_exc: urllib.error.URLError | None = None
    for attempt in range(MAX_FETCH_ATTEMPTS):
        try:
            h = hashlib.sha256()
            with urlopen(url) as response, tmp.open("wb") as f:
                while chunk := response.read(chunk_size):
                    h.update(chunk)
                    f.write(chunk)
            return tmp, "sha256:" + h.hexdigest()
        except urllib.error.URLError as exc:
            tmp.unlink(missing_ok=True)
            last_exc = exc
            client_error = (
                isinstance(exc, urllib.error.HTTPError)
                and 400 <= exc.code < 500
                and exc.code != 429
            )
            if client_error or attempt + 1 >= MAX_FETCH_ATTEMPTS:
                raise SyncError(f"failed to fetch {url}: {exc}") from exc
            time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    raise SyncError(f"failed to fetch {url}: {last_exc}")


def _update_manifest_digests(
    repo_root: Path,
    vendor_id: str,
    digest_updates: dict[str, str],
    manifest_path: Path | None = None,
) -> None:
    manifest_path = manifest_path or repo_root / MANIFEST_PATH
    raw = json.loads(manifest_path.read_text())
    for vendor_entry in raw["vendors"]:
        if vendor_entry["id"] == vendor_id:
            for file_entry in vendor_entry["files"]:
                target = file_entry.get("target")
                if target in digest_updates:
                    file_entry["digest"] = digest_updates[target]
            break
    tmp = manifest_path.with_name(manifest_path.name + ".sync-tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    tmp.replace(manifest_path)


def _sync_vendor_url_files(
    repo_root: Path,
    vendor: Vendor,
    *,
    check: bool,
    urlopen: UrlOpen = default_urlopen,
    manifest_path: Path | None = None,
) -> list[Path]:
    updated: list[Path] = []
    digest_updates: dict[str, str] = {}

    for vendor_file in vendor.files:
        target = resolve_target(repo_root, vendor_file.target)
        ensure_target_exists(target, vendor_file.target, vendor.id, check=check)
        stored_digest = require_string(vendor_file.digest, "digest")

        if check:
            actual_digest = sha256_digest_path(target)
            if actual_digest != stored_digest:
                raise DriftError(
                    f"{vendor.id}: drift detected for {vendor_file.target.as_posix()}"
                )
            if vendor_file.executable and not is_executable(target):
                raise DriftError(
                    f"{vendor.id}: executable drift detected for {vendor_file.target.as_posix()}"
                )
            continue

        fetched_tmp, new_digest = _fetch_to_temp(
            require_string(vendor_file.url, "url"), target, urlopen
        )
        tmp_consumed = False
        try:
            local_digest = sha256_digest_path(target)
            if local_digest != new_digest:
                fetched_tmp.replace(target)
                tmp_consumed = True
                if vendor_file.executable:
                    apply_executable_mode(target)
                updated.append(target)
            elif vendor_file.executable and not is_executable(target):
                apply_executable_mode(target)
                updated.append(target)

            if new_digest != stored_digest:
                digest_updates[vendor_file.target.as_posix()] = new_digest
        finally:
            if not tmp_consumed:
                fetched_tmp.unlink(missing_ok=True)

    if digest_updates:
        _update_manifest_digests(repo_root, vendor.id, digest_updates, manifest_path)
        updated.append(manifest_path or repo_root / MANIFEST_PATH)

    return updated


def sync_vendor(
    repo_root: Path,
    vendor: Vendor,
    *,
    check: bool,
    urlopen: UrlOpen = default_urlopen,
    manifest_path: Path | None = None,
) -> list[Path]:
    if vendor.fetch.type == "url-files":
        return _sync_vendor_url_files(
            repo_root, vendor, check=check, urlopen=urlopen, manifest_path=manifest_path
        )
    updated: list[Path] = []
    digest_updates: dict[str, str] = {}
    for item in plan_vendor_sync(vendor):
        target = resolve_target(repo_root, item.target)
        ensure_target_exists(target, item.target, vendor.id, check=check)
        if check and item.digest is not None:
            if sha256_digest_path(target) != item.digest:
                raise DriftError(
                    f"{vendor.id}: drift detected for {item.target.as_posix()}"
                )
            if item.executable and not is_executable(target):
                raise DriftError(
                    f"{vendor.id}: executable drift detected for {item.target.as_posix()}"
                )
            continue
        fetched_tmp, fetched_digest = _fetch_to_temp(item.url, target, urlopen)
        tmp_consumed = False
        try:
            local_digest = sha256_digest_path(target)
            if check:
                if local_digest != fetched_digest:
                    raise DriftError(
                        f"{vendor.id}: drift detected for {item.target.as_posix()}"
                    )
                if item.executable and not is_executable(target):
                    raise DriftError(
                        f"{vendor.id}: executable drift detected for {item.target.as_posix()}"
                    )
            else:
                if local_digest != fetched_digest:
                    fetched_tmp.replace(target)
                    tmp_consumed = True
                    if item.executable:
                        apply_executable_mode(target)
                    updated.append(target)
                elif item.executable and not is_executable(target):
                    apply_executable_mode(target)
                    updated.append(target)
                if fetched_digest != item.digest:
                    digest_updates[item.target.as_posix()] = fetched_digest
        finally:
            if not tmp_consumed:
                fetched_tmp.unlink(missing_ok=True)
    if digest_updates:
        _update_manifest_digests(repo_root, vendor.id, digest_updates, manifest_path)
        updated.append(manifest_path or repo_root / MANIFEST_PATH)
    return updated


STARTER_MANIFEST = """{
  "$schema": "https://raw.githubusercontent.com/forbish/vendored/main/schemas/vendored.schema.json",
  "vendors": [
    {
      "id": "sync-vendored-tool",
      "datasource": "github-releases",
      "depName": "forbish/vendored",
      "versioning": "semver",
      "currentValue": "v1.0.0",
      "fetch": { "type": "github-tagged-files", "repo": "forbish/vendored" },
      "files": [
        {
          "source": "tools/sync-vendored.py",
          "target": "tools/sync-vendored.py",
          "executable": true
        }
      ]
    }
  ]
}
"""

BOOTSTRAP_RENOVATE = """// Renovate config for a repo that consumes the vendored sync tool.
// Extend the shared preset, then run the sync command after upgrades.
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["github>forbish/vendored"],
  "postUpgradeTasks": {
    // Requires RENOVATE_ALLOWED_COMMANDS to allow this command on the bot.
    "commands": ["python3 tools/sync-vendored.py --id {{{depName}}}"],
    "fileFilters": ["vendored.json", "**"],
    "executionMode": "update"
  }
}
"""

# The Renovate preset consumers extend to pull in the custom manager + presets.
RENOVATE_PRESET = "github>forbish/vendored"

# Locations Renovate searches for a repository config, relative to repo root.
RENOVATE_CONFIG_LOCATIONS = (
    "renovate.json",
    "renovate.json5",
    ".github/renovate.json",
    ".github/renovate.json5",
    ".gitlab/renovate.json",
    ".renovaterc",
    ".renovaterc.json",
    ".renovaterc.json5",
)

BOOTSTRAP_WORKFLOW = """name: vendored-sync-check

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Verify vendored sources are in sync
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: python3 tools/sync-vendored.py --check
"""


def find_renovate_config(root: Path) -> Path | None:
    for rel in RENOVATE_CONFIG_LOCATIONS:
        candidate = root / rel
        if candidate.is_file():
            return candidate
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text())
        except (ValueError, OSError):
            data = None
        if isinstance(data, dict) and "renovate" in data:
            return package_json
    return None


def ensure_renovate_preset(root: Path) -> tuple[Path | None, str]:
    """Ensure an existing Renovate config extends the shared preset.

    Returns ``(config, action)`` where action is one of ``none`` (no config),
    ``unchanged`` (already wired), ``updated`` (preset inserted into plain
    JSON), or ``manual`` (a richer format that needs a hand edit to stay safe).
    """
    cfg = find_renovate_config(root)
    if cfg is None:
        return None, "none"

    text = cfg.read_text()
    if RENOVATE_PRESET in text:
        return cfg, "unchanged"

    # Only plain JSON round-trips losslessly through the stdlib; editing JSON5,
    # rc files, or package.json risks corrupting comments or unrelated keys.
    if cfg.suffix == ".json" and cfg.name != "package.json":
        data = json.loads(text)
        extends = data.get("extends")
        if extends is None:
            data["extends"] = [RENOVATE_PRESET]
        elif isinstance(extends, list):
            data["extends"] = [*extends, RENOVATE_PRESET]
        else:
            data["extends"] = [extends, RENOVATE_PRESET]
        cfg.write_text(json.dumps(data, indent=2) + "\n")
        return cfg, "updated"

    return cfg, "manual"


def write_bootstrap(root: Path) -> list[tuple[Path, str]]:
    """Idempotently scaffold the consumer-side files under ``root``.

    Existing files are never overwritten so the command is safe to re-run.
    A Renovate config is only written when the repo has none, to avoid
    creating a second, conflicting configuration.
    """
    actions: list[tuple[Path, str]] = []

    def ensure(path: Path, content: str) -> None:
        if path.exists():
            actions.append((path, "skipped"))
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        actions.append((path, "wrote"))

    ensure(root / MANIFEST_PATH, STARTER_MANIFEST)
    ensure(root / ".github" / "workflows" / "vendored-sync.yml", BOOTSTRAP_WORKFLOW)

    existing = find_renovate_config(root)
    if existing is None:
        ensure(root / ".github" / "renovate.json5", BOOTSTRAP_RENOVATE)
    else:
        actions.append((existing, "skipped"))
    return actions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync exact-copy vendored sources from upstream."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--id",
        dest="vendor_ids",
        action="append",
        help=(
            "Only sync vendors matching the given selector, compared against "
            "each vendor's id, depName, or packageName. Repeatable. Matching "
            "depName lets Renovate postUpgradeTasks call --id {{{depName}}}."
        ),
    )
    parser.add_argument(
        "--fetch-type",
        dest="fetch_types",
        action="append",
        help="Only sync vendors with the specified fetch type.",
    )
    parser.add_argument(
        "--root",
        help="Repository root containing the manifest (default: parent of tools/).",
    )
    parser.add_argument(
        "--manifest",
        help="Path to the manifest file (default: <root>/vendored.json).",
    )
    parser.add_argument(
        "--bootstrap",
        metavar="DIR",
        nargs="?",
        const=".",
        help=(
            "Idempotently scaffold a starter manifest, Renovate config, and "
            "sync workflow into DIR (default: current directory), then exit."
        ),
    )
    parser.add_argument(
        "--wire",
        metavar="DIR",
        nargs="?",
        const=".",
        help=(
            "Ensure an existing Renovate config in DIR (default: current "
            "directory) extends the shared preset, then exit. Plain JSON is "
            "edited in place; richer formats print guidance instead."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Check for vendored drift without rewriting files. "
            "Compares the local file hash against the stored digest offline; "
            "fetches from upstream only when no digest is stored yet."
        ),
    )
    return parser


def main(
    argv: list[str] | None = None,
    repo_root: Path | None = None,
    urlopen: UrlOpen | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.bootstrap is not None:
        for path, action in write_bootstrap(Path(args.bootstrap)):
            print(f"bootstrap: {action} {path.as_posix()}")
        return 0
    if args.wire is not None:
        cfg, action = ensure_renovate_preset(Path(args.wire))
        if action == "none":
            print("wire: no existing Renovate config found")
        elif action == "manual":
            assert cfg is not None
            print(
                f'wire: add "{RENOVATE_PRESET}" to the extends array in {cfg.as_posix()},'
            )
            print("  then add a postUpgradeTasks command:")
            print("    python3 tools/sync-vendored.py --id {{{depName}}}")
        else:
            assert cfg is not None
            print(f"wire: {action} {cfg.as_posix()}")
        return 0
    if repo_root is None:
        if args.root:
            repo_root = Path(args.root).resolve()
        elif args.manifest:
            repo_root = Path(args.manifest).resolve().parent
        else:
            repo_root = Path(__file__).resolve().parent.parent
    if urlopen is None:
        urlopen = default_urlopen
    manifest_path = (
        Path(args.manifest).resolve() if args.manifest else repo_root / MANIFEST_PATH
    )
    manifest = load_manifest(manifest_path)
    selected = manifest.select(args.vendor_ids, args.fetch_types)
    for vendor in selected:
        updated = sync_vendor(
            repo_root,
            vendor,
            check=args.check,
            urlopen=urlopen,
            manifest_path=manifest_path,
        )
        if args.check:
            print(f"{vendor.id}: ok")
        elif updated:
            for path in updated:
                print(f"{vendor.id}: updated {path.relative_to(repo_root).as_posix()}")
        else:
            print(f"{vendor.id}: no changes")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SyncError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
