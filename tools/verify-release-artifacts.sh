#!/bin/sh
set -eu

dir="."
repo="forbish/vendored"
tag=""
skip_attestation="false"

usage() {
	cat <<'EOF'
Usage: tools/verify-release-artifacts.sh [options]

Verify release artifacts produced by this repository's release workflow.

Options:
  --dir <path>              Directory containing release artifacts (default: .)
  --repo <owner/repo>       GitHub repository (default: forbish/vendored)
  --tag <tag>               Release tag (example: v1.0.0). Required.
  --skip-attestation        Skip GitHub attestation verification
  -h, --help                Show this help

Required files in <dir>:
  checksums.txt
  checksums.txt.sig
  checksums.txt.pem

This script verifies:
  1) keyless cosign signature on checksums.txt
  2) archive checksums listed in checksums.txt
  3) (optional) GitHub attestation for checksums.txt
EOF
}

while [ "$#" -gt 0 ]; do
	case "$1" in
		--dir)
			[ "$#" -ge 2 ] || {
				echo "error: --dir requires a value" >&2
				exit 2
			}
			dir="$2"
			shift 2
			;;
		--repo)
			[ "$#" -ge 2 ] || {
				echo "error: --repo requires a value" >&2
				exit 2
			}
			repo="$2"
			shift 2
			;;
		--tag)
			[ "$#" -ge 2 ] || {
				echo "error: --tag requires a value" >&2
				exit 2
			}
			tag="$2"
			shift 2
			;;
		--skip-attestation)
			skip_attestation="true"
			shift
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			echo "error: unknown argument: $1" >&2
			usage >&2
			exit 2
			;;
	esac
done

[ -n "$tag" ] || {
	echo "error: --tag is required" >&2
	exit 2
}

for f in checksums.txt checksums.txt.sig checksums.txt.pem; do
	[ -f "$dir/$f" ] || {
		echo "error: missing required file: $dir/$f" >&2
		exit 1
	}
done

command -v cosign >/dev/null 2>&1 || {
	echo "error: cosign is required" >&2
	exit 1
}

echo "[1/3] Verifying cosign signature on checksums.txt"
cosign verify-blob \
	--certificate "$dir/checksums.txt.pem" \
	--signature "$dir/checksums.txt.sig" \
	--certificate-identity "https://github.com/${repo}/.github/workflows/release.yml@refs/tags/${tag}" \
	--certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
	"$dir/checksums.txt" >/dev/null

echo "[2/3] Verifying file checksums"
if command -v sha256sum >/dev/null 2>&1; then
	(cd "$dir" && sha256sum --check checksums.txt)
elif command -v shasum >/dev/null 2>&1; then
	(cd "$dir" && shasum -a 256 --check checksums.txt)
else
	echo "error: need sha256sum or shasum to verify checksums" >&2
	exit 1
fi

if [ "$skip_attestation" = "true" ]; then
	echo "[3/3] Skipping attestation verification"
else
	command -v gh >/dev/null 2>&1 || {
		echo "error: gh CLI is required for attestation verification" >&2
		exit 1
	}
	echo "[3/3] Verifying GitHub attestation"
	gh attestation verify "$dir/checksums.txt" --repo "$repo"
fi

echo "release verification: ok"