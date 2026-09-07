# Security

Please use GitHub private vulnerability reporting for credential exposure, unauthorized uploads, destructive behavior, or malicious-file handling issues. Do not include real API keys, private media, network addresses or unredacted logs in public issues.

This app reads removable media and sends it to the configured Immich server. Credentials are stored in the macOS login Keychain and retrieved by the system `security` utility; they are never embedded in release binaries. Settings, cache and logs stay under the current user's Application Support directory.

The current 0.1.x series receives fixes on a best-effort basis. Downloads are ad-hoc signed, not Apple-notarized. Release checksums are published beside each ZIP. There is no automatic update mechanism.
