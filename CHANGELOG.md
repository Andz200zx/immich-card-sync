# Changelog

## 0.2.1

- retry newly uploaded RAWs while Immich is still extracting capture metadata
- keep completed metadata with missing capture details available for manual review

## 0.2.0

- add an optional Mac RAW development worker with a separate installer and login service
- develop full-resolution sRGB JPEG companions using RawTherapee and ExifTool
- preserve originals, capture metadata, archive visibility and import tags; use JPEG stack covers
- resume metadata scans and interrupted uploads with durable per-asset receipts
- skip existing JPEGs and preserve conflicting stacks for review
- bound conversion threads, batch duration, scratch space and retained logs
- include local sample rendering, status and targeted retry commands

## 0.1.1

- validate upgrades before replacing a working installation and restore it if activation fails
- migrate the original login agent so only one helper starts at login
- protect pending imports when changing server or account
- preserve existing release downloads when retrying the release workflow
- derive build versions from the app metadata by default

## 0.1.0

- initial public release with a universal macOS app and interactive installer
- prompted camera-card imports for photos and videos
- checksum checks, repeat imports and interrupted-transfer recovery
- RAW/JPEG stacking with JPEG covers
- upload-date tags with persistent retry receipts
- local and optional fallback server addresses
- Keychain credentials, login startup, tests and automated releases
