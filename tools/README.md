# tools/

| File               | Purpose                                                                                                                                       |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `sync-vendored.py` | Fetches the files declared in [`../vendored.json`](../vendored.json) at their pinned versions. See the [root README](../README.md) for usage. |
| `verify-release-artifacts.sh` | Verifies release `checksums.txt` via cosign keyless signature, checks archive hashes, and optionally verifies GitHub attestation. |
