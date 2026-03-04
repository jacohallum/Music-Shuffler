# iTunes Shuffler

A custom music player for large iTunes/M4A libraries with a smart shuffle algorithm, album artwork, global media keys, and a pre-conversion cache for DRM-free tracks.

## Features

- **Smart Shuffle** — weighted by play count, rating, recency, newness, loved status, and skip count
- **Background M4A pre-conversion** — converts tracks via FFmpeg so playback is instant
- **Unified metadata** — tracks play count, skip count, rating, and loved state for all 8K+ tracks
- **Album artwork** display
- **Up Next queue** with drag-free ordering
- **Global media keys** (F5–F8) work system-wide
- **Search & filter** across the full library

## Requirements

- Python 3.10+
- [pygame](https://pypi.org/project/pygame/) — audio playback
- [mutagen](https://pypi.org/project/mutagen/) — audio metadata
- [Pillow](https://pypi.org/project/Pillow/) — album artwork
- [FFmpeg](https://ffmpeg.org/download.html) — M4A/FLAC conversion (must be on PATH or configured)

```
pip install pygame mutagen Pillow
```

## Setup

1. Copy the config template and fill in your paths:
   ```
   cp config.example.json config.local.json
   ```
   Edit `config.local.json`:
   ```json
   {
       "music_dirs": [
           "C:\\Users\\YourName\\Music\\iTunes\\iTunes Media\\Music",
           "C:\\Users\\YourName\\Music\\M4P Downloads"
       ]
   }
   ```
   If no `config.local.json` is present, the app falls back to `~/Music/iTunes/iTunes Media/Music`.

2. Run the player:
   ```
   python music_player.py
   ```

## Privacy note

The `music_shuffler_cache/` directory contains your personal library index and listening history and is intentionally excluded from version control. Never commit it.

## Utilities

| Script | Purpose |
|--------|---------|
| `utlities/list_albums.py` | List albums ranked by play count, cross-referenced against a local download wishlist |
| `utlities/check_track_stats.py <path>` | Show Smart Shuffle weight breakdown for a specific track |
| `utlities/detect_media_keys.py` | Debug global media key detection |
| `scripts/preflight_privacy_check.sh` | Verify no personal paths/cache files would be committed before a push |
