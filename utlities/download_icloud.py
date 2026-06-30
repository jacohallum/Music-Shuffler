#!/usr/bin/env python3
"""
Trigger iCloud Music Library downloads via iTunes COM API.
Finds all tracks with no local file and calls Download() on each.

Usage:
    python utlities/download_icloud.py [--dry-run] [--batch N] [--delay S]

Options:
    --dry-run    List cloud tracks without downloading
    --batch N    Download N tracks then pause (default: 50)
    --delay S    Seconds between downloads (default: 0.3)
"""

import sys
import io
import time
import argparse

# Force UTF-8 output so track names with special chars don't crash
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

try:
    import win32com.client
except ImportError:
    print("ERROR: pywin32 not installed. Run: pip install pywin32")
    sys.exit(1)


def connect_itunes():
    try:
        # EnsureDispatch generates type lib wrappers — exposes vtable methods like Download()
        itunes = win32com.client.gencache.EnsureDispatch("iTunes.Application")
        return itunes
    except Exception as e:
        print(f"ERROR: Cannot connect to iTunes COM: {e}")
        print("Make sure iTunes (non-Store version) is running.")
        sys.exit(1)


def find_cloud_tracks(itunes):
    """Return list of tracks with no local file (cloud-only)."""
    library = itunes.LibraryPlaylist
    tracks = library.Tracks
    count = tracks.Count

    print(f"Scanning {count} tracks...")

    cloud = []
    local = 0
    errors = 0

    for i in range(1, count + 1):
        try:
            track = tracks.Item(i)
            location = ""
            try:
                location = track.Location
            except Exception:
                pass  # Some track types don't have Location

            if not location:
                cloud.append((i, track.Artist or "", track.Album or "", track.Name or ""))
            else:
                local += 1

            if i % 500 == 0:
                print(f"  {i}/{count} scanned, {len(cloud)} cloud so far...")

        except Exception:
            errors += 1

    print(f"\nScan complete:")
    print(f"  Local files:  {local}")
    print(f"  Cloud only:   {len(cloud)}")
    if errors:
        print(f"  Errors:       {errors}")

    return cloud


def download_tracks(itunes, cloud_indices, batch_size, delay):
    library = itunes.LibraryPlaylist
    tracks = library.Tracks

    total = len(cloud_indices)
    done = 0
    failed = 0

    print(f"\nStarting download of {total} tracks (batch={batch_size}, delay={delay}s)...")
    print("Press Ctrl+C to stop.\n")

    for idx, (track_idx, artist, album, name) in enumerate(cloud_indices, 1):
        try:
            track = tracks.Item(track_idx)
            track.Download()
            done += 1
            print(f"[{idx}/{total}] {artist} - {name} ({album})")
        except Exception as e:
            failed += 1
            import pywintypes
            if isinstance(e, pywintypes.com_error):
                err = f"COM {e.hresult:#010x}: {e.strerror}"
            else:
                err = repr(e)
            print(f"[{idx}/{total}] SKIP: {artist} - {name}: {err}")

        time.sleep(delay)

        # Pause between batches so iTunes doesn't choke
        if idx % batch_size == 0 and idx < total:
            print(f"\n--- Batch of {batch_size} done. Pausing 5s... ---\n")
            time.sleep(5)

    print(f"\nFinished: {done} queued, {failed} failed.")


def main():
    parser = argparse.ArgumentParser(description="Download iCloud tracks via iTunes COM")
    parser.add_argument("--dry-run", action="store_true", help="List only, no download")
    parser.add_argument("--batch", type=int, default=50, help="Tracks per batch (default 50)")
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds between downloads (default 0.3)")
    args = parser.parse_args()

    print("Connecting to iTunes...")
    itunes = connect_itunes()
    print(f"Connected: iTunes {itunes.Version}\n")

    cloud = find_cloud_tracks(itunes)

    if not cloud:
        print("\nAll tracks already downloaded locally.")
        return

    if args.dry_run:
        print(f"\n--- DRY RUN: {len(cloud)} cloud tracks ---")
        for _, artist, album, name in cloud[:50]:
            print(f"  {artist} - {name} ({album})")
        if len(cloud) > 50:
            print(f"  ... and {len(cloud) - 50} more")
        return

    try:
        download_tracks(itunes, cloud, args.batch, args.delay)
    except KeyboardInterrupt:
        print("\n\nStopped by user.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        input("\nPress Enter to close...")
