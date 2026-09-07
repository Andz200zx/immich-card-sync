# Contributing

Open an issue describing the camera, macOS version, Immich version and expected behavior before proposing large changes. Keep fixes focused; add regression tests for data handling or recovery changes.

Run `python3 -m unittest discover -s tests -v`. On a Mac, also run `./scripts/build.sh 0.1.0`. Tests use synthetic media and a local HTTP server. Never commit real camera files, logs, API keys, personal server configuration, or Keychain exports.

Preserve these properties: no card writes, no media deletion, checksum-based duplicate checks, conservative two-asset stacking, and recoverable tagging. Do not add telemetry or silently expand API-key permissions.

Commit and pull-request prose should be lower case, preserving proper nouns and code identifiers when needed. Contributions are under the MIT license.
