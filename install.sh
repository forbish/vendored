#!/bin/sh
# Idempotent installer for the forbish/vendored sync tool.
#
# Run from the root of the repository you want to add vendoring to:
#
#   curl -fsSL https://raw.githubusercontent.com/forbish/vendored/main/install.sh | sh
#
# Re-running is safe: it only writes what is missing, refreshes the tool when it
# differs, and never clobbers an existing Renovate config or manifest.
set -eu

REPO="forbish/vendored"
REF="${VENDORED_REF:-main}"
RAW="https://raw.githubusercontent.com/${REPO}/${REF}"
TOOL_PATH="tools/sync-vendored.py"

say() { printf '%s\n' "$*"; }

fetch() {
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL "$1"
	elif command -v wget >/dev/null 2>&1; then
		wget -qO- "$1"
	else
		say "error: need curl or wget to download the tool"
		exit 1
	fi
}

command -v python3 >/dev/null 2>&1 || {
	say "error: python3 is required"
	exit 1
}

# 1. Install or refresh the tool, only writing when the content differs.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
fetch "${RAW}/${TOOL_PATH}" >"$tmp"
head -n 1 "$tmp" | grep -q '^#!' || {
	say "error: downloaded tool does not look valid"
	exit 1
}
mkdir -p tools
if [ -f "$TOOL_PATH" ] && cmp -s "$tmp" "$TOOL_PATH"; then
	say "unchanged: $TOOL_PATH"
else
	cp "$tmp" "$TOOL_PATH"
	say "installed: $TOOL_PATH"
fi
chmod +x "$TOOL_PATH"

# 2. Scaffold the manifest, workflow, and (if absent) a Renovate config.
#    This step is idempotent and skips anything that already exists.
python3 "$TOOL_PATH" --bootstrap .

# 3. Wire an existing Renovate config, which --bootstrap deliberately leaves
#    untouched. Plain JSON is edited in place; richer formats get guidance so a
#    working config is never corrupted.
python3 "$TOOL_PATH" --wire .

say ""
say "Done. Next steps:"
say "  - Review vendored.json and add the upstreams you want to vendor."
say "  - Allow the sync command on your Renovate bot via RENOVATE_ALLOWED_COMMANDS:"
say "      ^python3 tools/sync-vendored\\.py( .*)?\$"
say "  - Commit the changes."
