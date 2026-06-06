# vendored-sources

Manifest-driven vendoring of upstream files, kept current by [Renovate].

You declare the upstream files you want to copy into your repo in a single
`vendored.json` manifest. A small, dependency-free Python tool
(`tools/sync-vendored.py`) fetches them at the pinned version. Renovate reads
the manifest, opens PRs when an upstream releases a new version, and re-runs the
sync tool so the vendored files update in the same PR.

It solves the problem of "I copied a file from another project and now it's
stale" — the copy is pinned, tracked, and updated automatically.

## Install

From the root of the repo you want to add vendoring to:

```sh
curl -fsSL https://raw.githubusercontent.com/forbish/vendored/main/install.sh | sh
```

[`install.sh`](install.sh) is idempotent and safe to re-run. It:

- installs (or refreshes) `tools/sync-vendored.py`,
- seeds a starter [`vendored.json`](vendored.json) that already vendors the tool
  itself, so the tool keeps itself up to date via Renovate,
- scaffolds a `.github/renovate.json5` and sync workflow when none exist, and
- wires an **existing** Renovate config to extend the preset — editing plain
  JSON in place and printing precise guidance for richer formats so a working
  config is never corrupted.

Prefer to do it by hand, or already have the tool vendored? Scaffold the same
files with `python3 tools/sync-vendored.py --bootstrap`, and wire an existing
Renovate config with `python3 tools/sync-vendored.py --wire`.

## How it works

```
vendored.json ──▶ Renovate (jsonata custom manager) ──▶ PR bumps currentValue
      │                                                        │
      │                                          postUpgradeTask runs
      ▼                                                        ▼
tools/sync-vendored.py ◀───────────────────────── re-fetches files at new version
      │
      ▼
files written to their target paths (committed in the same PR)
```

## The manifest: `vendored.json`

This repo's own manifest carries a single worked example: it vendors the
[Renovate config schema](https://docs.renovatebot.com/renovate-schema.json) into
[`schemas/renovate-schema.json`](schemas/renovate-schema.json) and references
that local copy from [`.github/renovate.json5`](.github/renovate.json5) — the
project dogfooding its own tool. When adopting this in another repo, replace that
entry with your own (or start from `vendors: []`).

Each entry under `vendors[]` describes one upstream source and the files to copy
from it. Validation is provided by
[`schemas/vendored.schema.json`](schemas/vendored.schema.json); the
manifest's `$schema` points at the published raw URL of that file, so a consuming
repo gets editor validation without copying the schema in.

| Field                                              | Purpose                                                             |
| -------------------------------------------------- | ------------------------------------------------------------------- |
| `id`                                               | Unique identifier for the entry (used by `--id`).                   |
| `datasource` / `depName` / `currentValue`          | What Renovate watches for new versions.                             |
| `currentDigest`                                    | Optional commit pin for ref-based sources.                          |
| `extractVersion`                                   | Optional regex to normalise upstream tags.                          |
| `fetch.type`                                       | How to fetch (see below).                                           |
| `fetch.repo` / `fetch.refTemplate` / `fetch.asset` | Fetch parameters.                                                   |
| `files[]`                                          | `source`/`url` → `target`, with optional `digest` and `executable`. |

### Fetch types

| `fetch.type`           | Fetches                              | Version source                           |
| ---------------------- | ------------------------------------ | ---------------------------------------- |
| `github-tagged-files`  | Files from a repo at a release tag   | `datasource: github-releases`            |
| `github-ref-files`     | Files from a repo at a branch/commit | `datasource: git-refs` + `currentDigest` |
| `github-release-asset` | A named release asset                | `datasource: github-releases`            |
| `url-files`            | Files from arbitrary URLs            | none — pinned by `digest`                |

A populated entry (here, `github-release-asset`) looks like:

```json
{
  "id": "example-binary",
  "datasource": "github-releases",
  "depName": "owner/repo",
  "versioning": "semver",
  "currentValue": "v1.2.3",
  "fetch": {
    "type": "github-release-asset",
    "repo": "owner/repo",
    "refTemplate": "{{version}}",
    "asset": "tool-linux-amd64"
  },
  "files": [{ "target": "bin/tool", "executable": true }]
}
```

## The sync tool: `tools/sync-vendored.py`

Pure Python 3, no third-party dependencies.

```sh
# Fetch everything in the manifest
python3 tools/sync-vendored.py

# Fetch a single entry
python3 tools/sync-vendored.py --id <id>

# Restrict to certain fetch types (used by Renovate's post-upgrade task)
python3 tools/sync-vendored.py --fetch-type github-tagged-files --fetch-type url-files

# Verify the working tree matches the manifest without writing (CI / pre-commit)
python3 tools/sync-vendored.py --check

# Point at a manifest outside the default location
python3 tools/sync-vendored.py --root /path/to/repo
python3 tools/sync-vendored.py --manifest /path/to/vendored.json

# Scaffold Renovate config + a sync workflow into a target directory
python3 tools/sync-vendored.py --bootstrap .github

# Print the tool version
python3 tools/sync-vendored.py --version
```

`--check` exits non-zero on drift, so it doubles as a CI guard. It compares each
local file against the `digest` stored in the manifest **offline** (no network).
The sync tool records a content digest for every fetch type after it writes a
file, so once an entry has been synced once, `--check` needs no network access
or token. Only entries that have never been synced (no stored digest) fall back
to fetching from upstream to compare.

### Authentication

For private repos or to avoid GitHub's unauthenticated rate limit (60 req/hr),
the tool sends a bearer token when one is present in the environment. It checks
`GITHUB_TOKEN`, then `RENOVATE_TOKEN`, then `GH_TOKEN`. The token is only
attached to requests to GitHub hosts and is **not** forwarded to redirect
targets (such as the presigned URLs that release-asset downloads redirect to),
so credentials are not leaked cross-host.

```sh
GITHUB_TOKEN=ghp_... python3 tools/sync-vendored.py
```

Renovate already runs with a token in its environment, so the post-upgrade task
picks this up automatically.

## Wiring it into Renovate

The fastest path is the **shared preset** shipped at the repo root
([`default.json`](default.json)). Extend it from your repo's Renovate config to
pull in the `jsonata` custom manager without copying it:

```json
{
  "extends": ["github>forbish/vendored"]
}
```

You still provide two repo-specific pieces the preset cannot supply for you:

- a `postUpgradeTasks` block that runs the sync command and lists your
  `files[].target` paths in `fileFilters`, and
- `RENOVATE_ALLOWED_COMMANDS` (self-hosted env) allow-listing that command.

Run `python3 tools/sync-vendored.py --bootstrap <dir>` to scaffold starter
versions of both files.

If you prefer to inline everything instead of extending the preset, the two
pieces are:

1. **Repo config** ([`examples/renovate.json5`](examples/renovate.json5)) — a
   `jsonata` custom manager that reads versions out of `vendored.json`, plus a
   `packageRule` whose `postUpgradeTasks` re-runs the sync tool.

2. **Self-hosted config** ([`examples/renovate-workflow.yml`](examples/renovate-workflow.yml))
   — `RENOVATE_ALLOWED_COMMANDS` must allow-list the sync command, or
   `postUpgradeTasks` is silently skipped.

### Two gotchas worth knowing

- **`managerFilePatterns` are globs by default.** A bare string like
  `(^|/)vendored\.json$` is treated as a *glob* and matches nothing. Use a glob
  (`**/vendored.json`) or wrap a regex in slashes (`/(^|/)vendored\.json$/`).
  The glob form is recommended.

- **`allowedCommands` is anchored.** `["^python3 tools/sync-vendored\\.py$"]`
  blocks the tool the moment you pass arguments. Use
  `["^python3 tools/sync-vendored\\.py( .*)?$"]`.

## Adding a new source

1. Add an entry to `vendored.json` (`id`, `datasource`/`depName`,
   `currentValue`, `fetch`, `files`).
2. Run `python3 tools/sync-vendored.py --id <id>` to fetch it once.
3. Add the new `files[].target` paths to the `fileFilters` in your Renovate
   `postUpgradeTasks` so the bump PR commits them.
4. Commit the manifest and the fetched files.

## Keeping the tooling itself current

The sync tool is itself a file copied into your repo — the exact thing this
project exists to keep from going stale. (The schema is *referenced* by URL, not
copied, so it stays current automatically.) Once this repo is published with
release tags, point your manifest at it so Renovate updates the tool for you, the
same way it updates everything else:

```json
{
  "id": "vendored-sources",
  "datasource": "github-releases",
  "depName": "forbish/vendored",
  "versioning": "semver",
  "currentValue": "v1.0.0",
  "fetch": {
    "type": "github-tagged-files",
    "repo": "forbish/vendored",
    "refTemplate": "{{version}}"
  },
  "files": [
    { "source": "tools/sync-vendored.py", "target": "tools/sync-vendored.py", "executable": true }
  ]
}
```

## Verifying release artifacts

Release tags publish six assets:

- `vendored-vX.Y.Z.tar.gz`
- `vendored-vX.Y.Z.zip`
- `checksums.txt`
- `checksums.txt.sig`
- `checksums.txt.pem`
- `sbom.spdx.json`

After downloading those files, verify in this order:

### One command (recommended)

```sh
tools/verify-release-artifacts.sh --dir <download-dir> --repo forbish/vendored --tag vX.Y.Z
```

The release workflow also runs this verifier before publishing release assets.

### Manual verification steps

1. **Verify the keyless cosign signature over `checksums.txt`:**

```sh
cosign verify-blob \
  --certificate checksums.txt.pem \
  --signature checksums.txt.sig \
  --certificate-identity-regexp "https://github.com/forbish/vendored/.github/workflows/release.yml@refs/tags/v.*" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  checksums.txt
```

2. **Verify archive integrity against the signed checksums file:**

```sh
sha256sum --check checksums.txt
```

3. **(Optional) Verify GitHub build provenance attestation for `checksums.txt`:**

```sh
gh attestation verify checksums.txt --repo forbish/vendored
```

If all checks pass, the archives and SBOM are tied to the release workflow run
that produced them.

## Tests

```sh
python3 -m pytest tests/
```

[Renovate]: https://docs.renovatebot.com/
