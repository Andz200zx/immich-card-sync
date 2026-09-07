A lightweight menu-bar app for importing camera photos and videos into Immich.

Download the universal ZIP for Apple Silicon and Intel Macs. Requires macOS 13+ and an installed Python 3.10+ runtime. Extract it, run **Install Immich Card Sync.command**, and supply your Immich URL and API key. The built app is included; no Xcode installation is needed.

The app prompts on card insertion, skips files already present, stacks RAW/JPEG pairs with JPEG covers, and tags new batches by upload date. See the README for API-key permissions and recovery behavior.

The binary is ad-hoc signed and is not Apple-notarized. macOS may require explicit approval before opening it. SHA-256 checksums are included. This is an initial release targeting the Immich v3.1 API; test it with a card you have backed up.
