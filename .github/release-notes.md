adds an optional RAW-to-JPEG worker for a processing Mac, alongside the existing camera-card importer.

- develops full-resolution JPEG companions for RAWs without JPEG equivalents, with verified JPEG stack covers
- supports resumable existing-library backfill and checks for new uploads in small batches
- preserves originals, resolved capture times, archive visibility and import tags
- leaves existing JPEGs, ambiguous matches and conflicting stacks for review
- processes one RAW at a time, with bounded working space and retained logs

download the universal ZIP and read `RAW_WORKER.md` before running **Install RAW Worker.command** on the processing Mac. the optional worker needs Python, RawTherapee and ExifTool. the official RawTherapee 5.13 Apple Silicon package requires macOS Tahoe 26 or newer. the card importer retains its macOS 13 / Python 3.10 minimum requirements.

the built card app is included for Apple Silicon and Intel. it is ad-hoc signed, not Apple-notarized; SHA-256 checksums accompany the download. camera support depends on the installed RAW decoder. existing installations keep their separate configurations and progress.
