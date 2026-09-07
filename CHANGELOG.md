# Changelog

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
