import email.message
import hashlib
import importlib.util
import json
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "tools" / "sync-vendored.py"
spec = importlib.util.spec_from_file_location("sync_vendored", MODULE_PATH)
assert spec is not None
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

DriftError = module.DriftError
SelectionError = module.SelectionError
SyncError = module.SyncError
SyncPlanItem = module.SyncPlanItem
build_parser = module.build_parser
build_request = module.build_request
github_token = module.github_token
is_executable = module.is_executable
load_manifest = module.load_manifest
main = module.main
plan_vendor_sync = module.plan_vendor_sync
sync_vendor = module.sync_vendor


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size == -1:
            chunk = self.payload[self._offset :]
            self._offset = len(self.payload)
        else:
            chunk = self.payload[self._offset : self._offset + size]
            self._offset += len(chunk)
        return chunk

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def touch(path: Path, content: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_build_request_adds_token_for_github_hosts() -> None:
    request = build_request(
        "https://raw.githubusercontent.com/owner/repo/v1/file", token="secret"
    )
    assert request.get_header("Authorization") == "Bearer secret"


def test_build_request_skips_token_for_other_hosts() -> None:
    request = build_request("https://example.com/file", token="secret")
    assert request.get_header("Authorization") is None


def test_build_request_no_token_no_header() -> None:
    request = build_request(
        "https://github.com/owner/repo/releases/download/v1/asset", token=None
    )
    assert request.get_header("Authorization") is None


def test_github_token_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("RENOVATE_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert github_token() is None
    monkeypatch.setenv("GH_TOKEN", "from-gh")
    assert github_token() == "from-gh"
    monkeypatch.setenv("GITHUB_TOKEN", "from-github")
    assert github_token() == "from-github"


def test_load_manifest_parses_supported_entries(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "$schema": "./.vscode/schemas/vendored-schema.json",
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                        "refTemplate": "{{version}}",
                    },
                    "files": [
                        {
                            "source": "config/core.config.toml",
                            "target": "stacks/compute/devops/config/komodo-core/core.config.example.toml",
                        }
                    ],
                    "updateGroup": "komodo",
                },
                {
                    "id": "sh-helpers",
                    "datasource": "github-releases",
                    "depName": "forbish/sh-helpers",
                    "versioning": "semver",
                    "currentValue": "v1.0.0",
                    "fetch": {
                        "type": "github-release-asset",
                        "repo": "forbish/sh-helpers",
                        "asset": "sh-helpers",
                    },
                    "files": [{"target": "tools/sh-helpers", "executable": True}],
                },
                {
                    "id": "compose-spec-schema",
                    "datasource": "git-refs",
                    "depName": "compose-spec/compose-spec",
                    "packageName": "https://github.com/compose-spec/compose-spec",
                    "versioning": "git",
                    "currentValue": "main",
                    "currentDigest": "14a4f1c4c8bfc195aa365bdd329bc2f33204d733",
                    "fetch": {
                        "type": "github-ref-files",
                        "repo": "compose-spec/compose-spec",
                        "refTemplate": "{{version}}",
                    },
                    "files": [
                        {
                            "source": "schema/compose-spec.json",
                            "target": ".vscode/schemas/compose-spec.json",
                        }
                    ],
                },
                {
                    "id": "authentik-blueprint-schema",
                    "datasource": "github-releases",
                    "depName": "goauthentik/authentik",
                    "versioning": "semver",
                    "currentValue": "2026.2.2",
                    "extractVersion": "^version/(?<version>.*)$",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "goauthentik/authentik",
                        "refTemplate": "version/{{version}}",
                    },
                    "files": [
                        {
                            "source": "blueprints/schema.json",
                            "target": ".vscode/schemas/authentik-schema.json",
                        }
                    ],
                },
            ],
        },
    )

    manifest = load_manifest(manifest_path)

    assert [vendor.id for vendor in manifest.vendors] == [
        "komodo",
        "sh-helpers",
        "compose-spec-schema",
        "authentik-blueprint-schema",
    ]
    assert manifest.vendor("komodo").fetch.ref_for_version("v2.1.1") == "v2.1.1"
    assert manifest.vendor("sh-helpers").files[0].executable is True
    assert (
        manifest.vendor("compose-spec-schema").package_name
        == "https://github.com/compose-spec/compose-spec"
    )
    assert (
        manifest.vendor("compose-spec-schema").current_digest
        == "14a4f1c4c8bfc195aa365bdd329bc2f33204d733"
    )
    assert (
        manifest.vendor("authentik-blueprint-schema").extract_version
        == "^version/(?<version>.*)$"
    )


def test_load_manifest_rejects_invalid_tagged_file_entry(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                    },
                    "files": [
                        {
                            "target": "stacks/compute/devops/config/komodo-core/core.config.example.toml"
                        }
                    ],
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="source"):
        load_manifest(manifest_path)


def test_load_manifest_rejects_target_paths_outside_repo(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                    },
                    "files": [
                        {
                            "source": "config/core.config.toml",
                            "target": "../outside.toml",
                        }
                    ],
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="inside the repository"):
        load_manifest(manifest_path)


def test_load_manifest_rejects_source_paths_outside_upstream_ref(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                    },
                    "files": [
                        {
                            "source": "../config/core.config.toml",
                            "target": "stacks/compute/devops/config/komodo-core/core.config.example.toml",
                        }
                    ],
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="relative upstream path"):
        load_manifest(manifest_path)


def test_load_manifest_requires_versioning(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                        "refTemplate": "{{version}}",
                    },
                    "files": [
                        {
                            "source": "config/core.config.toml",
                            "target": "stacks/compute/devops/config/komodo-core/core.config.example.toml",
                        }
                    ],
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="versioning"):
        load_manifest(manifest_path)


def test_load_manifest_requires_current_digest_for_ref_files(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "compose-spec-schema",
                    "datasource": "git-refs",
                    "depName": "compose-spec/compose-spec",
                    "packageName": "https://github.com/compose-spec/compose-spec",
                    "versioning": "git",
                    "currentValue": "main",
                    "fetch": {
                        "type": "github-ref-files",
                        "repo": "compose-spec/compose-spec",
                        "refTemplate": "{{version}}",
                    },
                    "files": [
                        {
                            "source": "schema/compose-spec.json",
                            "target": ".vscode/schemas/compose-spec.json",
                        }
                    ],
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="currentDigest"):
        load_manifest(manifest_path)


def test_plan_vendor_sync_builds_tagged_file_downloads(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                        "refTemplate": "{{version}}",
                    },
                    "files": [
                        {
                            "source": "config/core.config.toml",
                            "target": "stacks/compute/devops/config/komodo-core/core.config.example.toml",
                        },
                        {
                            "source": "config/periphery.config.toml",
                            "target": "stacks/compute/devops/config/periphery/periphery.config.example.toml",
                        },
                    ],
                }
            ]
        },
    )

    manifest = load_manifest(manifest_path)

    assert plan_vendor_sync(manifest.vendor("komodo")) == [
        SyncPlanItem(
            url="https://raw.githubusercontent.com/moghtech/komodo/v2.1.1/config/core.config.toml",
            target=Path(
                "stacks/compute/devops/config/komodo-core/core.config.example.toml"
            ),
            executable=False,
        ),
        SyncPlanItem(
            url="https://raw.githubusercontent.com/moghtech/komodo/v2.1.1/config/periphery.config.toml",
            target=Path(
                "stacks/compute/devops/config/periphery/periphery.config.example.toml"
            ),
            executable=False,
        ),
    ]


def test_plan_vendor_sync_quotes_raw_github_url_path_parts(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "sample",
                    "datasource": "github-releases",
                    "depName": "owner/repo",
                    "versioning": "semver",
                    "currentValue": "v1.0.0",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "owner/repo",
                        "refTemplate": "release/{{version}}",
                    },
                    "files": [
                        {
                            "source": "config files/example #1.toml",
                            "target": "stacks/example/config.toml",
                        }
                    ],
                }
            ]
        },
    )

    manifest = load_manifest(manifest_path)

    assert plan_vendor_sync(manifest.vendor("sample")) == [
        SyncPlanItem(
            url=(
                "https://raw.githubusercontent.com/owner/repo/release/v1.0.0/"
                "config%20files/example%20%231.toml"
            ),
            target=Path("stacks/example/config.toml"),
            executable=False,
        )
    ]


def test_plan_vendor_sync_builds_ref_file_downloads_from_current_digest(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "compose-spec-schema",
                    "datasource": "git-refs",
                    "depName": "compose-spec/compose-spec",
                    "packageName": "https://github.com/compose-spec/compose-spec",
                    "versioning": "git",
                    "currentValue": "main",
                    "currentDigest": "14a4f1c4c8bfc195aa365bdd329bc2f33204d733",
                    "fetch": {
                        "type": "github-ref-files",
                        "repo": "compose-spec/compose-spec",
                        "refTemplate": "{{version}}",
                    },
                    "files": [
                        {
                            "source": "schema/compose-spec.json",
                            "target": ".vscode/schemas/compose-spec.json",
                        }
                    ],
                }
            ]
        },
    )

    manifest = load_manifest(manifest_path)

    assert plan_vendor_sync(manifest.vendor("compose-spec-schema")) == [
        SyncPlanItem(
            url=(
                "https://raw.githubusercontent.com/compose-spec/compose-spec/"
                "14a4f1c4c8bfc195aa365bdd329bc2f33204d733/schema/compose-spec.json"
            ),
            target=Path(".vscode/schemas/compose-spec.json"),
            executable=False,
        )
    ]


def test_sync_vendor_writes_tagged_files_as_exact_bytes(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                        "refTemplate": "{{version}}",
                    },
                    "files": [
                        {
                            "source": "config/core.config.toml",
                            "target": "stacks/compute/devops/config/komodo-core/core.config.example.toml",
                        }
                    ],
                }
            ]
        },
    )
    manifest = load_manifest(manifest_path)
    payload = b"exact upstream bytes\nwith spacing preserved\n"
    seen_urls: list[str] = []
    touch(
        repo_root / "stacks/compute/devops/config/komodo-core/core.config.example.toml",
        b"old\n",
    )

    def fake_urlopen(url: str) -> FakeResponse:
        seen_urls.append(url)
        return FakeResponse(payload)

    sync_vendor(repo_root, manifest.vendor("komodo"), check=False, urlopen=fake_urlopen)

    assert seen_urls == [
        "https://raw.githubusercontent.com/moghtech/komodo/v2.1.1/config/core.config.toml"
    ]
    assert (
        repo_root / "stacks/compute/devops/config/komodo-core/core.config.example.toml"
    ).read_bytes() == payload


def test_sync_vendor_writes_release_asset_and_sets_executable_bit(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "sh-helpers",
                    "datasource": "github-releases",
                    "depName": "forbish/sh-helpers",
                    "versioning": "semver",
                    "currentValue": "v1.0.0",
                    "fetch": {
                        "type": "github-release-asset",
                        "repo": "forbish/sh-helpers",
                        "asset": "sh-helpers",
                    },
                    "files": [{"target": "tools/sh-helpers", "executable": True}],
                }
            ]
        },
    )
    manifest = load_manifest(manifest_path)
    payload = b"#!/bin/sh\nprintf 'hello\\n'\n"
    touch(repo_root / "tools/sh-helpers", b"#!/bin/sh\n")

    def fake_urlopen(url: str) -> FakeResponse:
        assert (
            url
            == "https://github.com/forbish/sh-helpers/releases/download/v1.0.0/sh-helpers"
        )
        return FakeResponse(payload)

    sync_vendor(
        repo_root, manifest.vendor("sh-helpers"), check=False, urlopen=fake_urlopen
    )

    target = repo_root / "tools/sh-helpers"
    assert target.read_bytes() == payload
    assert target.stat().st_mode & stat.S_IXUSR


def test_sync_vendor_write_mode_rejects_missing_declared_target(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                        "refTemplate": "{{version}}",
                    },
                    "files": [
                        {
                            "source": "config/core.config.toml",
                            "target": "stacks/compute/devops/config/komodo-core/core.config.example.toml",
                        }
                    ],
                }
            ]
        },
    )
    manifest = load_manifest(manifest_path)
    called = False

    def fake_urlopen(url: str) -> FakeResponse:
        nonlocal called
        called = True
        return FakeResponse(b"fresh\n")

    with pytest.raises(SyncError, match="missing target"):
        sync_vendor(
            repo_root, manifest.vendor("komodo"), check=False, urlopen=fake_urlopen
        )

    assert called is False
    assert not (repo_root / "stacks/compute/devops/config/komodo-core").exists()


def test_sync_vendor_check_detects_content_drift(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                        "refTemplate": "{{version}}",
                    },
                    "files": [
                        {
                            "source": "config/core.config.toml",
                            "target": "stacks/compute/devops/config/komodo-core/core.config.example.toml",
                        }
                    ],
                }
            ]
        },
    )
    manifest = load_manifest(manifest_path)
    target = (
        repo_root / "stacks/compute/devops/config/komodo-core/core.config.example.toml"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"stale\n")

    with pytest.raises(DriftError, match="drift"):
        sync_vendor(
            repo_root,
            manifest.vendor("komodo"),
            check=True,
            urlopen=lambda url: FakeResponse(b"fresh\n"),
        )


def test_sync_vendor_check_detects_missing_executable_bit(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "sh-helpers",
                    "datasource": "github-releases",
                    "depName": "forbish/sh-helpers",
                    "versioning": "semver",
                    "currentValue": "v1.0.0",
                    "fetch": {
                        "type": "github-release-asset",
                        "repo": "forbish/sh-helpers",
                        "asset": "sh-helpers",
                    },
                    "files": [{"target": "tools/sh-helpers", "executable": True}],
                }
            ]
        },
    )
    manifest = load_manifest(manifest_path)
    target = repo_root / "tools/sh-helpers"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"#!/bin/sh\n")
    target.chmod(0o644)

    with pytest.raises(DriftError, match="executable"):
        sync_vendor(
            repo_root,
            manifest.vendor("sh-helpers"),
            check=True,
            urlopen=lambda url: FakeResponse(b"#!/bin/sh\n"),
        )


def test_manifest_vendor_selection_rejects_unknown_ids(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                    },
                    "files": [
                        {
                            "source": "config/core.config.toml",
                            "target": "stacks/compute/devops/config/komodo-core/core.config.example.toml",
                        }
                    ],
                }
            ]
        },
    )

    manifest = load_manifest(manifest_path)

    with pytest.raises(SelectionError, match="unknown vendor id"):
        manifest.select(["sh-helpers"])


RENOVATE_URL_VENDOR = {
    "id": "renovate-schema",
    "fetch": {
        "type": "url-files",
    },
    "files": [
        {
            "url": "https://docs.renovatebot.com/renovate-schema.json",
            "target": ".vscode/schemas/renovate-schema.json",
            "digest": "sha256:" + hashlib.sha256(b"schema-content\n").hexdigest(),
        },
        {
            "url": "https://docs.renovatebot.com/renovate-global-schema.json",
            "target": ".vscode/schemas/renovate-global-schema.json",
            "digest": "sha256:" + hashlib.sha256(b"global-content\n").hexdigest(),
        },
    ],
}


def test_load_manifest_parses_url_files_entry(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(manifest_path, {"vendors": [RENOVATE_URL_VENDOR]})

    manifest = load_manifest(manifest_path)

    vendor = manifest.vendor("renovate-schema")
    assert vendor.fetch.type == "url-files"
    assert vendor.fetch.repo is None
    assert vendor.datasource is None
    assert vendor.dep_name is None
    assert vendor.versioning is None
    assert vendor.current_value is None
    assert len(vendor.files) == 2
    assert vendor.files[0].url == "https://docs.renovatebot.com/renovate-schema.json"
    assert (
        vendor.files[1].url
        == "https://docs.renovatebot.com/renovate-global-schema.json"
    )
    assert vendor.files[0].digest is not None
    assert vendor.files[0].digest.startswith("sha256:")


def test_load_manifest_rejects_url_files_entry_without_url(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    **RENOVATE_URL_VENDOR,
                    "files": [
                        {
                            "target": ".vscode/schemas/renovate-schema.json",
                            "digest": "sha256:" + "a" * 64,
                        }
                    ],
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="url"):
        load_manifest(manifest_path)


def test_load_manifest_rejects_url_files_entry_without_digest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    **RENOVATE_URL_VENDOR,
                    "files": [
                        {
                            "url": "https://docs.renovatebot.com/renovate-schema.json",
                            "target": ".vscode/schemas/renovate-schema.json",
                        }
                    ],
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="digest"):
        load_manifest(manifest_path)


def test_sync_vendor_url_files_writes_changed_content(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [RENOVATE_URL_VENDOR]})
    manifest = load_manifest(manifest_path)

    upstream_schema = b'{"$schema": "http://json-schema.org/draft-07/schema#"}\n'
    upstream_global = (
        b'{"$schema": "http://json-schema.org/draft-07/schema#", "global": true}\n'
    )
    url_responses = {
        "https://docs.renovatebot.com/renovate-schema.json": upstream_schema,
        "https://docs.renovatebot.com/renovate-global-schema.json": upstream_global,
    }
    touch(repo_root / ".vscode/schemas/renovate-schema.json", b"schema-content\n")
    touch(
        repo_root / ".vscode/schemas/renovate-global-schema.json", b"global-content\n"
    )

    sync_vendor(
        repo_root,
        manifest.vendor("renovate-schema"),
        check=False,
        urlopen=lambda url: FakeResponse(url_responses[url]),
    )

    assert (
        repo_root / ".vscode/schemas/renovate-schema.json"
    ).read_bytes() == upstream_schema
    assert (
        repo_root / ".vscode/schemas/renovate-global-schema.json"
    ).read_bytes() == upstream_global

    updated_manifest = json.loads((repo_root / "vendored.json").read_text())
    files = updated_manifest["vendors"][0]["files"]
    import hashlib as _hashlib

    assert (
        files[0]["digest"] == "sha256:" + _hashlib.sha256(upstream_schema).hexdigest()
    )
    assert (
        files[1]["digest"] == "sha256:" + _hashlib.sha256(upstream_global).hexdigest()
    )


def test_sync_vendor_url_files_no_op_when_content_unchanged(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [RENOVATE_URL_VENDOR]})
    manifest = load_manifest(manifest_path)

    # Return content whose digest matches the stored digest
    url_responses = {
        "https://docs.renovatebot.com/renovate-schema.json": b"schema-content\n",
        "https://docs.renovatebot.com/renovate-global-schema.json": b"global-content\n",
    }
    touch(repo_root / ".vscode/schemas/renovate-schema.json", b"schema-content\n")
    touch(
        repo_root / ".vscode/schemas/renovate-global-schema.json", b"global-content\n"
    )
    original_manifest = (repo_root / "vendored.json").read_bytes()

    sync_vendor(
        repo_root,
        manifest.vendor("renovate-schema"),
        check=False,
        urlopen=lambda url: FakeResponse(url_responses[url]),
    )

    # Manifest should be untouched when nothing changed
    assert (repo_root / "vendored.json").read_bytes() == original_manifest


def test_sync_vendor_url_files_restores_local_content_when_upstream_is_unchanged(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [RENOVATE_URL_VENDOR]})
    manifest = load_manifest(manifest_path)

    url_responses = {
        "https://docs.renovatebot.com/renovate-schema.json": b"schema-content\n",
        "https://docs.renovatebot.com/renovate-global-schema.json": b"global-content\n",
    }
    touch(repo_root / ".vscode/schemas/renovate-schema.json", b"tampered\n")
    touch(
        repo_root / ".vscode/schemas/renovate-global-schema.json", b"global-content\n"
    )
    original_manifest = manifest_path.read_bytes()

    updated = sync_vendor(
        repo_root,
        manifest.vendor("renovate-schema"),
        check=False,
        urlopen=lambda url: FakeResponse(url_responses[url]),
    )

    assert (
        repo_root / ".vscode/schemas/renovate-schema.json"
    ).read_bytes() == b"schema-content\n"
    assert (
        repo_root / ".vscode/schemas/renovate-global-schema.json"
    ).read_bytes() == b"global-content\n"
    assert manifest_path.read_bytes() == original_manifest
    assert updated == [repo_root / ".vscode/schemas/renovate-schema.json"]


def test_sync_vendor_url_files_check_is_local_only(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [RENOVATE_URL_VENDOR]})
    manifest = load_manifest(manifest_path)

    touch(repo_root / ".vscode/schemas/renovate-schema.json", b"schema-content\n")
    touch(
        repo_root / ".vscode/schemas/renovate-global-schema.json", b"global-content\n"
    )

    fetch_called = False

    def should_not_fetch(url: str) -> FakeResponse:
        nonlocal fetch_called
        fetch_called = True
        return FakeResponse(b"")

    sync_vendor(
        repo_root,
        manifest.vendor("renovate-schema"),
        check=True,
        urlopen=should_not_fetch,
    )

    assert not fetch_called


def test_sync_vendor_url_files_check_detects_content_drift(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [RENOVATE_URL_VENDOR]})
    manifest = load_manifest(manifest_path)

    # Local file has different content than what the stored digest represents
    touch(repo_root / ".vscode/schemas/renovate-schema.json", b"tampered\n")
    touch(
        repo_root / ".vscode/schemas/renovate-global-schema.json", b"global-content\n"
    )

    with pytest.raises(DriftError, match="drift"):
        sync_vendor(
            repo_root,
            manifest.vendor("renovate-schema"),
            check=True,
            urlopen=lambda url: FakeResponse(b""),
        )


def test_sync_vendor_url_files_updates_only_changed_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [RENOVATE_URL_VENDOR]})
    manifest = load_manifest(manifest_path)

    new_schema = b'{"$schema": "updated"}\n'
    unchanged_global = b"global-content\n"
    url_responses = {
        "https://docs.renovatebot.com/renovate-schema.json": new_schema,
        "https://docs.renovatebot.com/renovate-global-schema.json": unchanged_global,
    }
    touch(repo_root / ".vscode/schemas/renovate-schema.json", b"schema-content\n")
    touch(repo_root / ".vscode/schemas/renovate-global-schema.json", unchanged_global)

    updated = sync_vendor(
        repo_root,
        manifest.vendor("renovate-schema"),
        check=False,
        urlopen=lambda url: FakeResponse(url_responses[url]),
    )

    assert (
        repo_root / ".vscode/schemas/renovate-schema.json"
    ).read_bytes() == new_schema
    assert (
        repo_root / ".vscode/schemas/renovate-global-schema.json"
    ).read_bytes() == unchanged_global

    updated_manifest = json.loads(manifest_path.read_text())
    files = updated_manifest["vendors"][0]["files"]
    assert files[0]["digest"] == "sha256:" + hashlib.sha256(new_schema).hexdigest()
    assert (
        files[1]["digest"] == "sha256:" + hashlib.sha256(unchanged_global).hexdigest()
    )

    assert repo_root / ".vscode/schemas/renovate-schema.json" in updated
    assert repo_root / ".vscode/schemas/renovate-global-schema.json" not in updated


def test_sync_vendor_url_files_sets_executable_bit(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    payload_old = b"#!/bin/sh\nold\n"
    payload_new = b"#!/bin/sh\nnew\n"
    vendor = {
        "id": "exec-script",
        "fetch": {"type": "url-files"},
        "files": [
            {
                "url": "https://example.com/script.sh",
                "target": "tools/script.sh",
                "digest": "sha256:" + hashlib.sha256(payload_old).hexdigest(),
                "executable": True,
            }
        ],
    }
    write_json(manifest_path, {"vendors": [vendor]})
    manifest = load_manifest(manifest_path)
    touch(repo_root / "tools/script.sh", payload_old)

    sync_vendor(
        repo_root,
        manifest.vendor("exec-script"),
        check=False,
        urlopen=lambda url: FakeResponse(payload_new),
    )

    target = repo_root / "tools/script.sh"
    assert target.read_bytes() == payload_new
    assert is_executable(target)


def test_sync_vendor_url_files_check_detects_missing_executable_bit(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    payload = b"#!/bin/sh\nold\n"
    vendor = {
        "id": "exec-script",
        "fetch": {"type": "url-files"},
        "files": [
            {
                "url": "https://example.com/script.sh",
                "target": "tools/script.sh",
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "executable": True,
            }
        ],
    }
    write_json(manifest_path, {"vendors": [vendor]})
    manifest = load_manifest(manifest_path)
    target = repo_root / "tools/script.sh"
    touch(target, payload)
    target.chmod(0o644)

    with pytest.raises(DriftError, match="executable"):
        sync_vendor(
            repo_root,
            manifest.vendor("exec-script"),
            check=True,
            urlopen=lambda url: FakeResponse(b""),
        )


def test_sync_vendor_url_files_restores_executable_bit_when_content_is_unchanged(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    payload = b"#!/bin/sh\nold\n"
    vendor = {
        "id": "exec-script",
        "fetch": {"type": "url-files"},
        "files": [
            {
                "url": "https://example.com/script.sh",
                "target": "tools/script.sh",
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "executable": True,
            }
        ],
    }
    write_json(manifest_path, {"vendors": [vendor]})
    manifest = load_manifest(manifest_path)
    target = repo_root / "tools/script.sh"
    touch(target, payload)
    target.chmod(0o644)
    original_manifest = manifest_path.read_bytes()

    updated = sync_vendor(
        repo_root,
        manifest.vendor("exec-script"),
        check=False,
        urlopen=lambda url: FakeResponse(payload),
    )

    assert target.read_bytes() == payload
    assert is_executable(target)
    assert manifest_path.read_bytes() == original_manifest
    assert updated == [target]


def test_select_raises_when_id_filtered_by_fetch_type(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                        "refTemplate": "{{version}}",
                    },
                    "files": [{"source": "cfg.toml", "target": "stacks/cfg.toml"}],
                },
                RENOVATE_URL_VENDOR,
            ]
        },
    )
    manifest = load_manifest(manifest_path)

    with pytest.raises(SelectionError, match="komodo"):
        manifest.select(["komodo"], ["url-files"])

    result = manifest.select(["renovate-schema"], ["url-files"])
    assert len(result) == 1 and result[0].id == "renovate-schema"


def test_select_resolves_dep_name_and_package_name(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                    },
                    "files": [{"source": "cfg.toml", "target": "stacks/cfg.toml"}],
                },
                {
                    "id": "compose-spec-schema",
                    "datasource": "github-tags",
                    "depName": "compose-spec/compose-spec",
                    "packageName": "https://github.com/compose-spec/compose-spec",
                    "versioning": "semver",
                    "currentValue": "v1.0.0",
                    "fetch": {
                        "type": "github-ref-files",
                        "repo": "compose-spec/compose-spec",
                        "refTemplate": "master",
                    },
                    "currentDigest": "0" * 40,
                    "files": [{"source": "schema.json", "target": "schema.json"}],
                },
            ]
        },
    )
    manifest = load_manifest(manifest_path)

    # depName resolves to the vendor (the postUpgradeTask `--id {{{depName}}}` path).
    by_dep_name = manifest.select(["moghtech/komodo"])
    assert len(by_dep_name) == 1 and by_dep_name[0].id == "komodo"

    # packageName also resolves.
    by_package = manifest.select(["https://github.com/compose-spec/compose-spec"])
    assert len(by_package) == 1 and by_package[0].id == "compose-spec-schema"

    # id still resolves.
    by_id = manifest.select(["komodo"])
    assert len(by_id) == 1 and by_id[0].id == "komodo"


def test_select_dep_name_matching_multiple_vendors(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored.json"
    write_json(
        manifest_path,
        {
            "vendors": [
                {
                    "id": "komodo-core",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                    },
                    "files": [{"source": "core.toml", "target": "core.toml"}],
                },
                {
                    "id": "komodo-periphery",
                    "datasource": "github-releases",
                    "depName": "moghtech/komodo",
                    "versioning": "semver",
                    "currentValue": "v2.1.1",
                    "fetch": {
                        "type": "github-tagged-files",
                        "repo": "moghtech/komodo",
                    },
                    "files": [{"source": "periphery.toml", "target": "periphery.toml"}],
                },
            ]
        },
    )
    manifest = load_manifest(manifest_path)

    # A shared depName that Renovate bumps together must re-sync every vendor.
    result = manifest.select(["moghtech/komodo"])
    assert {v.id for v in result} == {"komodo-core", "komodo-periphery"}


def test_main_prints_no_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = tmp_path
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [RENOVATE_URL_VENDOR]})

    touch(repo_root / ".vscode/schemas/renovate-schema.json", b"schema-content\n")
    touch(
        repo_root / ".vscode/schemas/renovate-global-schema.json", b"global-content\n"
    )

    url_responses = {
        "https://docs.renovatebot.com/renovate-schema.json": b"schema-content\n",
        "https://docs.renovatebot.com/renovate-global-schema.json": b"global-content\n",
    }

    rc = main(
        ["--fetch-type", "url-files"],
        repo_root=repo_root,
        urlopen=lambda url: FakeResponse(url_responses[url]),
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "renovate-schema: no changes" in out


def test_main_prints_updated_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = tmp_path
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [RENOVATE_URL_VENDOR]})

    touch(repo_root / ".vscode/schemas/renovate-schema.json", b"schema-content\n")
    touch(
        repo_root / ".vscode/schemas/renovate-global-schema.json", b"global-content\n"
    )

    new_content = b'{"updated": true}\n'
    url_responses = {
        "https://docs.renovatebot.com/renovate-schema.json": new_content,
        "https://docs.renovatebot.com/renovate-global-schema.json": b"global-content\n",
    }

    rc = main(
        ["--fetch-type", "url-files"],
        repo_root=repo_root,
        urlopen=lambda url: FakeResponse(url_responses[url]),
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "renovate-schema: updated .vscode/schemas/renovate-schema.json" in out


def test_main_with_unknown_id_raises_selection_error(tmp_path: Path) -> None:
    repo_root = tmp_path
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [RENOVATE_URL_VENDOR]})

    with pytest.raises(SelectionError, match="unknown vendor id"):
        main(["--id", "does-not-exist"], repo_root=repo_root)


GITHUB_VENDOR = {
    "id": "komodo",
    "datasource": "github-releases",
    "depName": "moghtech/komodo",
    "versioning": "semver",
    "currentValue": "v2.1.1",
    "fetch": {
        "type": "github-tagged-files",
        "repo": "moghtech/komodo",
        "refTemplate": "{{version}}",
    },
    "files": [{"source": "cfg.toml", "target": "stacks/cfg.toml"}],
}
GITHUB_URL = "https://raw.githubusercontent.com/moghtech/komodo/v2.1.1/cfg.toml"


def _github_manifest_with_digest(content: bytes) -> dict:
    vendor = json.loads(json.dumps(GITHUB_VENDOR))
    vendor["files"][0]["digest"] = "sha256:" + hashlib.sha256(content).hexdigest()
    return {"vendors": [vendor]}


def test_fetch_retries_on_transient_url_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [GITHUB_VENDOR]})
    manifest = load_manifest(manifest_path)
    content = b"komodo config\n"
    touch(repo_root / "stacks/cfg.toml", b"old\n")

    calls = {"n": 0}

    def flaky(url: str) -> FakeResponse:
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("transient")
        return FakeResponse(content)

    sync_vendor(repo_root, manifest.vendor("komodo"), check=False, urlopen=flaky)

    assert calls["n"] == 3
    assert (repo_root / "stacks/cfg.toml").read_bytes() == content


def test_fetch_gives_up_after_max_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [GITHUB_VENDOR]})
    manifest = load_manifest(manifest_path)
    touch(repo_root / "stacks/cfg.toml", b"old\n")

    calls = {"n": 0}

    def always_fail(url: str) -> FakeResponse:
        calls["n"] += 1
        raise urllib.error.URLError("down")

    with pytest.raises(SyncError, match="failed to fetch"):
        sync_vendor(
            repo_root, manifest.vendor("komodo"), check=False, urlopen=always_fail
        )

    assert calls["n"] == module.MAX_FETCH_ATTEMPTS


def test_fetch_does_not_retry_client_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [GITHUB_VENDOR]})
    manifest = load_manifest(manifest_path)
    touch(repo_root / "stacks/cfg.toml", b"old\n")

    calls = {"n": 0}

    def not_found(url: str) -> FakeResponse:
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    with pytest.raises(SyncError):
        sync_vendor(
            repo_root, manifest.vendor("komodo"), check=False, urlopen=not_found
        )

    assert calls["n"] == 1


def test_sync_vendor_github_check_is_offline_when_digest_stored(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    content = b"komodo config\n"
    write_json(manifest_path, _github_manifest_with_digest(content))
    manifest = load_manifest(manifest_path)
    touch(repo_root / "stacks/cfg.toml", content)

    def boom(url: str) -> FakeResponse:
        raise AssertionError("network should not be used when a digest is stored")

    sync_vendor(repo_root, manifest.vendor("komodo"), check=True, urlopen=boom)


def test_sync_vendor_github_check_detects_drift_offline(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, _github_manifest_with_digest(b"komodo config\n"))
    manifest = load_manifest(manifest_path)
    touch(repo_root / "stacks/cfg.toml", b"tampered\n")

    with pytest.raises(DriftError, match="drift"):
        sync_vendor(
            repo_root,
            manifest.vendor("komodo"),
            check=True,
            urlopen=lambda url: FakeResponse(b""),
        )


def test_sync_vendor_github_stores_digest_on_write(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "vendored.json"
    write_json(manifest_path, {"vendors": [GITHUB_VENDOR]})
    manifest = load_manifest(manifest_path)
    content = b"komodo config\n"
    touch(repo_root / "stacks/cfg.toml", b"old\n")

    updated = sync_vendor(
        repo_root,
        manifest.vendor("komodo"),
        check=False,
        urlopen=lambda url: FakeResponse(content),
    )

    assert (repo_root / "stacks/cfg.toml").read_bytes() == content
    stored = json.loads(manifest_path.read_text())
    digest = stored["vendors"][0]["files"][0]["digest"]
    assert digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert manifest_path in updated


def test_load_manifest_raises_on_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "vendored.json"
    path.write_text("{ not valid json")

    with pytest.raises(SyncError, match="invalid JSON"):
        load_manifest(path)


def test_load_manifest_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(SyncError, match="manifest not found"):
        load_manifest(tmp_path / "missing.json")


def test_main_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert module.__version__ in capsys.readouterr().out


def test_main_manifest_override(tmp_path: Path) -> None:
    custom = tmp_path / "nested" / "manifest.json"
    write_json(custom, {"vendors": [RENOVATE_URL_VENDOR]})
    touch(tmp_path / "nested/.vscode/schemas/renovate-schema.json", b"schema-content\n")
    touch(
        tmp_path / "nested/.vscode/schemas/renovate-global-schema.json",
        b"global-content\n",
    )

    rc = main(
        ["--manifest", str(custom), "--check"],
        urlopen=lambda url: FakeResponse(b""),
    )

    assert rc == 0


def test_main_root_override(tmp_path: Path) -> None:
    write_json(tmp_path / "vendored.json", {"vendors": [RENOVATE_URL_VENDOR]})
    touch(tmp_path / ".vscode/schemas/renovate-schema.json", b"schema-content\n")
    touch(tmp_path / ".vscode/schemas/renovate-global-schema.json", b"global-content\n")

    rc = main(
        ["--root", str(tmp_path), "--check"],
        urlopen=lambda url: FakeResponse(b""),
    )

    assert rc == 0


def test_redirect_handler_strips_authorization_cross_host() -> None:
    handler = module._AuthStrippingRedirectHandler()
    req = urllib.request.Request(
        "https://github.com/owner/repo", headers={"Authorization": "Bearer secret"}
    )

    cross = handler.redirect_request(
        req,
        None,
        302,
        "Found",
        email.message.Message(),
        "https://objects.githubusercontent.com/blob",
    )
    same = handler.redirect_request(
        req,
        None,
        302,
        "Found",
        email.message.Message(),
        "https://github.com/owner/other",
    )

    assert cross is not None and cross.get_header("Authorization") is None
    assert same is not None and same.get_header("Authorization") == "Bearer secret"


def test_write_bootstrap_creates_files(tmp_path: Path) -> None:
    actions = dict(module.write_bootstrap(tmp_path))

    manifest = tmp_path / "vendored.json"
    renovate = tmp_path / ".github" / "renovate.json5"
    workflow = tmp_path / ".github" / "workflows" / "vendored-sync.yml"
    assert actions[manifest] == "wrote"
    assert actions[renovate] == "wrote"
    assert actions[workflow] == "wrote"
    assert "sync-vendored-tool" in manifest.read_text()
    assert "github>forbish/vendored" in renovate.read_text()
    assert "sync-vendored.py --check" in workflow.read_text()


def test_write_bootstrap_seeds_valid_starter_manifest(tmp_path: Path) -> None:
    module.write_bootstrap(tmp_path)

    manifest = json.loads((tmp_path / "vendored.json").read_text())
    vendors = manifest["vendors"]
    assert vendors[0]["id"] == "sync-vendored-tool"
    assert vendors[0]["files"][0]["target"] == "tools/sync-vendored.py"
    # The starter manifest must load through the real parser.
    parsed = module.load_manifest(tmp_path / "vendored.json")
    assert parsed.vendors[0].id == "sync-vendored-tool"


def test_write_bootstrap_is_idempotent(tmp_path: Path) -> None:
    module.write_bootstrap(tmp_path)
    renovate = tmp_path / ".github" / "renovate.json5"
    renovate.write_text("// user edits\n{}\n")

    actions = dict(module.write_bootstrap(tmp_path))

    assert actions[tmp_path / "vendored.json"] == "skipped"
    assert actions[renovate] == "skipped"
    # The user's edits survive a re-run.
    assert renovate.read_text() == "// user edits\n{}\n"


def test_write_bootstrap_skips_renovate_when_config_exists(tmp_path: Path) -> None:
    existing = tmp_path / "renovate.json"
    existing.write_text("{}\n")

    actions = dict(module.write_bootstrap(tmp_path))

    assert actions[existing] == "skipped"
    assert not (tmp_path / ".github" / "renovate.json5").exists()


def test_find_renovate_config_detects_locations(tmp_path: Path) -> None:
    assert module.find_renovate_config(tmp_path) is None

    cfg = tmp_path / ".github" / "renovate.json5"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}\n")
    assert module.find_renovate_config(tmp_path) == cfg


def test_find_renovate_config_detects_package_json_key(tmp_path: Path) -> None:
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"renovate": {"extends": []}}))
    assert module.find_renovate_config(tmp_path) == pkg


def test_ensure_renovate_preset_none(tmp_path: Path) -> None:
    cfg, action = module.ensure_renovate_preset(tmp_path)
    assert cfg is None
    assert action == "none"


def test_ensure_renovate_preset_inserts_into_plain_json(tmp_path: Path) -> None:
    cfg = tmp_path / "renovate.json"
    cfg.write_text(json.dumps({"extends": ["config:recommended"]}))

    result, action = module.ensure_renovate_preset(tmp_path)

    assert result == cfg
    assert action == "updated"
    data = json.loads(cfg.read_text())
    assert data["extends"] == ["config:recommended", "github>forbish/vendored"]


def test_ensure_renovate_preset_creates_extends_when_absent(tmp_path: Path) -> None:
    cfg = tmp_path / "renovate.json"
    cfg.write_text(json.dumps({"labels": ["deps"]}))

    _, action = module.ensure_renovate_preset(tmp_path)

    assert action == "updated"
    assert json.loads(cfg.read_text())["extends"] == ["github>forbish/vendored"]


def test_ensure_renovate_preset_unchanged_when_present(tmp_path: Path) -> None:
    cfg = tmp_path / "renovate.json"
    cfg.write_text(json.dumps({"extends": ["github>forbish/vendored"]}))

    _, action = module.ensure_renovate_preset(tmp_path)

    assert action == "unchanged"


def test_ensure_renovate_preset_manual_for_json5(tmp_path: Path) -> None:
    cfg = tmp_path / ".github" / "renovate.json5"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('// comment\n{ "extends": ["config:recommended"] }\n')

    result, action = module.ensure_renovate_preset(tmp_path)

    assert result == cfg
    assert action == "manual"
    # The file must be left untouched.
    assert "config:recommended" in cfg.read_text()
    assert "forbish/vendored" not in cfg.read_text()


def test_ensure_renovate_preset_manual_for_package_json(tmp_path: Path) -> None:
    cfg = tmp_path / "package.json"
    cfg.write_text(json.dumps({"renovate": {"extends": []}}))

    result, action = module.ensure_renovate_preset(tmp_path)

    assert result == cfg
    assert action == "manual"


def test_main_wire_updates_plain_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "renovate.json").write_text(json.dumps({"extends": []}))

    rc = main(["--wire"])

    assert rc == 0
    data = json.loads((tmp_path / "renovate.json").read_text())
    assert data["extends"] == ["github>forbish/vendored"]


def test_main_bootstrap_defaults_to_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rc = main(["--bootstrap"])

    assert rc == 0
    assert (tmp_path / "vendored.json").exists()
    assert (tmp_path / ".github" / "renovate.json5").exists()
    assert (tmp_path / ".github" / "workflows" / "vendored-sync.yml").exists()
