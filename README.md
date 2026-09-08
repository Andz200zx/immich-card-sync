# Immich Card Sync

**Insert a camera card. Click Sync. Find the batch in Immich.**

A small macOS menu-bar app that imports camera photos and videos into your own [Immich](https://immich.app) server. It runs locally, with no AI service, subscription, telemetry, or whole-library scan.

[Download the latest Mac release](https://github.com/Andz200zx/immich-card-sync/releases/latest) · [Report a bug](https://github.com/Andz200zx/immich-card-sync/issues)

## What it does

- Prompts once when a camera card is inserted; **Not now** dismisses it until reinsertion.
- Uploads original photos **and videos**: Sony ARW/JPEG, DJI DNG/JPEG, MP4, MOV, Sony AVCHD MTS/M2TS, and other supported formats.
- Checks file content against Immich before uploading. Reinserting a card skips originals already present.
- Stacks exact RAW/JPEG pairs with the JPEG as cover, using folder, filename stem and matching capture metadata. Existing stacks containing other assets are left for review.
- Tags new uploads **SD imports/YYYY-MM-DD**, using the local date when the batch starts. In Immich, open **Tags → SD imports → date**. Multiple cards uploaded that day share a tag.
- Tries your primary server, then an optional fallback such as a Tailscale address.
- Retries pending tags and stacks after interrupted requests or delayed metadata extraction.
- Keeps your card originals untouched. No formatting, card deletion, media replacement or album creation.

The app listens for macOS volume events while idle. Python runs only during a sync or while finishing pending work. It never sends your media to this project or to GitHub.

For generating JPEG companions from an existing library, use the separate [Immich RAW Worker](https://github.com/Andz200zx/immich-raw-worker) project. It has its own download and installer.

## Download and install

1. Download the **universal ZIP** from [Releases](https://github.com/Andz200zx/immich-card-sync/releases/latest), then extract it. It contains the built app for **Apple Silicon and Intel Macs**.
2. Have **macOS 13 or newer** and **Python 3.10 or newer** installed. Homebrew Python or the python.org macOS installer works; Xcode is not needed for the download.
3. In Immich, create an API key with the permissions below.
4. Double-click **Install Immich Card Sync.command**. Enter your server URL, optional fallback URL, and API key when prompted. Key entry is hidden; it is stored in macOS Keychain, not in the configuration file.
5. Approve macOS Keychain/removable-volume access if requested. Insert a camera card and click **Sync**. Wait for completion before ejecting it in Finder.

Release binaries are **ad-hoc signed, not Apple-notarized**. macOS may require you to explicitly approve opening the downloaded installer/app in Privacy & Security. No instructions or scripts disable Gatekeeper. SHA-256 checksums accompany each release; they verify download integrity, not Apple notarization.

This initial release targets the Immich **v3.1 API**. The originating installation was checked against v3.1.0; compatibility with other versions is not guaranteed.

### API-key permissions

Use a key belonging to the account receiving the imports:

```
asset.read
asset.upload
server.about
user.read
stack.create
stack.read
stack.update
tag.create
tag.asset
```

Asset deletion, downloads, administrator/job management, albums, and sharing permissions are not required. Use a trusted server URL; HTTP is supported for a trusted LAN, while HTTPS is recommended when available. Turn on Tailscale before using a Tailscale fallback.

## Daily use and recovery

The SD-card menu contains manual sync, **Verify card contents and sync…**, progress, logs, and pause controls. The app starts at login; quitting stops it until the next login. **Pause card prompts** persists between logins.

If a transfer stops, reconnect and sync again. Completed originals are checked by checksum; an incomplete file restarts. Cached checksums speed up unchanged cards, and the **Verify** command rereads every file. Upload presence is always checked against Immich.

A successful file sync may leave RAW/JPEG stacks waiting for Immich’s metadata jobs. Those finish automatically while the app runs, even after card removal. Existing conflicting stacks or mismatched capture metadata are reported for review. If an original is in Immich’s bin, restore it before retrying.

Camera XML, subtitles, XMP sidecars and low-resolution proxies such as DJI `.LRF` are not imported. This is a media-library importer, not a full card-image backup. The Mac is kept awake during a sync, but closing its lid can interrupt networking.

## Settings and uninstalling

- App: `~/Applications/Immich Card Sync.app`
- Configuration: `~/Library/Application Support/Immich Card Sync/config.json`
- Hash cache, pending work and latest result: the `state/` directory beside the configuration
- Logs: the `logs/` directory, retaining the latest 30 run logs
- Login agent: `~/Library/LaunchAgents/io.github.andz200zx.immich-card-sync.plist`

To reconnect to another server/account, run the installer from Terminal with `--configure`. Finish pending imports before switching accounts. The installer checks the new setup before replacing a working installation and restores the previous app and login setup if activation fails. Keep configuration and state private: they contain server addresses, file names and asset identifiers, although not the API key.

**Uninstall Immich Card Sync.command** disables login startup. Quit the app if it was launched manually. The app, settings, logs and Keychain entry are retained; remove those yourself only if you no longer need them. No library or card files are removed.

## Build and test

Building requires macOS with Xcode or Command Line Tools, plus Python 3.10+ for tests. No pip packages are needed.

```sh
python3 -m unittest discover -s tests -v
./scripts/build.sh
```

The build uses the version in `src/Info.plist` unless a version argument is supplied. It cross-compiles Swift for arm64 and x86_64, combines them into a universal app, ad-hoc signs it, and creates a ZIP plus checksums in `dist/`. The Python backend is included as source in the app bundle; the installer records the installed Python executable path.

Tests use a temporary local HTTP server. They exercise repeated imports, streamed upload bytes, interrupted responses, date tags, two-camera filename collisions, changed files, JPEG covers, pending metadata, conflicting stacks, cancellation and permanent server failures. Tests never connect to a personal Immich server. A macOS volume-event diagnostic is available as `"Immich Card Sync.app/Contents/MacOS/Immich Card Sync" --observe-card-mount`.

CI runs the tests and builds the universal app. Pushing a `vX.Y.Z` tag runs the release workflow and publishes the ZIP and checksums automatically. Rerunning a published release preserves its existing downloads.

A maintainer can also publish a locally tested build if hosted runners are unavailable:

```sh
./scripts/build.sh 0.2.2
gh release create v0.2.2 dist/Immich-Card-Sync-0.2.2-universal.zip dist/Immich-Card-Sync-0.2.2-SHA256SUMS.txt --verify-tag --title v0.2.2 --notes-file .github/release-notes.md
```

Push the matching source tag before using `--verify-tag`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports belong in [private vulnerability reporting](https://github.com/Andz200zx/immich-card-sync/security/advisories/new), not public logs containing credentials. See [SECURITY.md](SECURITY.md).

MIT licensed. Independent community project; not affiliated with the Immich project.
