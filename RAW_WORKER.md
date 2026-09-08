# Optional RAW-to-JPEG worker

Run this on a processing Mac, such as an M1 Mac mini. The card helper can remain on your MacBook. Originals and generated JPEGs stay in your own Immich library; only temporary working files and a progress database live on the processing Mac. No AI service or subscription is used.

The worker reads RAW originals through the Immich API, develops their sensor data at full resolution, copies capture metadata, uploads the JPEG, verifies its checksum, and creates a two-asset stack with the JPEG first. Existing import tags are copied to the JPEG, plus a `RAW renders/YYYY-MM-DD` tag for the conversion batch. JPEG quality is 95, with 4:4:4 chroma and an embedded sRGB profile. Camera white balance and RawTherapee's ISO-appropriate auto-matched curve provide a camera-like starting point. Development cannot recover motion blur or exactly reproduce every camera's JPEG look.

## Requirements

- macOS with Python 3.10 or newer, RawTherapee CLI and ExifTool.
- The official RawTherapee 5.13 Apple Silicon build needs macOS Tahoe 26 or newer. See [its release page](https://rawtherapee.com/downloads/5.13/) for platform requirements. The card helper itself still requires only macOS 13.
- A logged-in macOS account for the login service. Locking the screen is fine. After a reboot, the account must log in before its LaunchAgent starts.
- An Immich v3.1-compatible API key for the account receiving the JPEGs:

```
asset.read
asset.download
asset.upload
user.read
stack.read
stack.create
stack.update
tag.asset
tag.create
```

No deletion or administrator permission is needed. The installer keeps this worker's key in a mode-600 file inside its private Application Support directory, suitable for unattended runs. The card helper continues using Keychain. Keep the worker's configuration, state and logs private; they contain library metadata and asset identifiers. Never commit them or include them in public issue attachments.

## Install

Install the dependencies on the processing Mac, for example with Homebrew:

```sh
brew install python@3.13 exiftool
brew install --cask rawtherapee
```

Download and extract the latest project release, then double-click **Install RAW Worker.command**. Enter your Immich URL, optional fallback and API key. This enables the background worker and its existing-library backfill. Use the same Immich account as your card imports. The worker needs network access to that server; enable Tailscale when using a Tailscale-only address.

To install for manual sample checks before enabling background work, run from the extracted package:

```sh
python3 scripts/install_worker.py
```

Then use the commands below. Run the installer again with `--enable` when ready. Reinstalling reuses the existing credentials and progress. An active batch must finish before an upgrade can replace its files.

## Operation and controls

Installed location: `~/Library/Application Support/Immich RAW Worker`. Settings are in `config.json`; progress is in `state/worker.sqlite3` and `state/last-result.json`; daily logs are in `logs/`. Commands below assume the Homebrew Python 3.13 used above. Use your installed Python path if different.

```sh
support="$HOME/Library/Application Support/Immich RAW Worker"
python=/opt/homebrew/bin/python3.13

# Read progress; this does not contact Immich.
"$python" "$support/bin/raw_worker.py" --config "$support/config.json" --state "$support/state" status

# Scan a few pages without downloading, uploading or stacking any media.
"$python" "$support/bin/raw_worker.py" --config "$support/config.json" --state "$support/state" scan --scan-pages 4

# Render one local sample without uploading it. Copy an asset UUID from its Immich URL.
# A separate sample state keeps it separate from the background queue.
"$python" "$support/bin/raw_worker.py" --config "$support/config.json" --state "$support/samples" sample --asset-id ASSET_UUID

# Retry one reviewed photo after correcting its metadata or stack in Immich.
"$python" "$support/bin/raw_worker.py" --config "$support/config.json" --state "$support/state" retry --asset-id ASSET_UUID
```

The worker checks for new RAWs every five minutes while logged in. Each run scans at most 12 pages of 100 RAWs, checks at most 250 queued assets, and stops after 25 conversions or 15 minutes between photos. Only one RAW conversion runs at a time, with two processing threads by default. It keeps the Mac awake during each batch, not between batches. Closing a remote SSH session does not stop the login service. Interrupted transfers and delayed Immich metadata jobs resume automatically. The first backfill can take days on a large library; subsequent scans use creation-time checkpoints and a weekly full sweep.

At least 8 GiB must remain free before preparing a photo; each RAW is capped at 1 GiB by default. Scratch files are removed only after the server copy, stack cover and tags are confirmed. Review cases retain a bounded text log, not a permanent extra RAW copy. Daily service logs retain 30 days; conversion review logs retain the latest 100 entries. Manual sample files remain until you remove them.

Pause until the next login:

```sh
launchctl bootout "gui/$(id -u)/io.github.andz200zx.immich-raw-worker"
```

Resume:

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/io.github.andz200zx.immich-raw-worker.plist"
```

To uninstall the login service, pause it and remove that specific plist. Settings, progress, generated JPEGs and RAW originals are retained.

## Matching and review

Extensions considered are ARW, NEF, CR2, DNG, CR3, NRW, CRW, RAF, RW2, ORF, PEF and SRW. Actual support depends on the camera and RAW encoding; the worker never substitutes an enlarged thumbnail when decoding fails. Output dimensions are checked against RAW metadata, allowing small sensor-border/crop differences and portrait rotation.

An existing JPEG in a RAW's stack is sufficient to skip it. For unstacked RAWs, filename, capture time and camera metadata are checked in both the timeline and archive. Renamed JPEG candidates, missing metadata, and existing stacks without JPEGs are reported for review. The worker preserves those stacks rather than dropping their members. Hidden, locked, trashed and offline assets are not processed. It preserves archive visibility during upload.

The receipt stores the RAW ID and checksum, JPEG checksum, renderer/profile identity and upload result. A lost upload response is checked by checksum before retrying, so it does not cause another upload. After metadata extraction, only the verified RAW and JPEG may form the new stack. A competing JPEG or changed stack encountered during processing is left for review. Completed conversions are not automatically regenerated when profiles change or when you deliberately remove a companion.

This is a companion-image workflow, not an editing or backup replacement. Existing JPEGs, RAW originals, cards, unrelated stacks and server thumbnail settings are never overwritten or deleted. Generated JPEGs remain independent Immich assets even if you later uninstall the worker.
