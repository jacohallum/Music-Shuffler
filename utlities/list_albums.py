#!/usr/bin/env python3
"""
Music Library Album List Generator with Priority Ranking
Reads from iTunes Library.plist since .m4p files don't have embedded metadata
"""

import os
import plistlib
from pathlib import Path
from collections import defaultdict
from urllib.parse import unquote, urlparse

# Configuration - set these to your local paths, or use config.local.json in project root
def _load_config():
    config_path = Path(__file__).parent.parent / "config.local.json"
    if config_path.exists():
        import json
        with open(config_path, 'r') as f:
            return json.load(f)
    return {}

_cfg = _load_config()
_home = Path.home()

MUSIC_DIRS = _cfg.get("music_dirs", [
    str(_home / "Music" / "iTunes" / "iTunes Media" / "Apple Music")
])

# Try both possible XML filenames
ITUNES_LIBRARY_XML = str(_home / "Music" / "iTunes" / "iTunes Library.xml")
ITUNES_MUSIC_LIBRARY_XML = str(_home / "Music" / "iTunes" / "iTunes Music Library.xml")

# Albums still needed - load from a local file if available, otherwise empty set
# To use: create albums_needed.txt with one "Artist\tAlbum" per line
def _load_albums_needed():
    needed_path = Path(__file__).parent / "albums_needed.txt"
    if not needed_path.exists():
        return set()
    albums = set()
    with open(needed_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and '\t' in line:
                artist, album = line.split('\t', 1)
                albums.add((artist, album))
    return albums

ALBUMS_NEEDED = _load_albums_needed()

AUDIO_EXTENSIONS = {'.m4a', '.m4p', '.mp3', '.flac', '.wav', '.aac', '.alac'}

def load_itunes_library():
    """Load iTunes Library XML and extract track data"""
    
    # Try both possible XML filenames
    xml_path = None
    if os.path.exists(ITUNES_LIBRARY_XML):
        xml_path = ITUNES_LIBRARY_XML
    elif os.path.exists(ITUNES_MUSIC_LIBRARY_XML):
        xml_path = ITUNES_MUSIC_LIBRARY_XML
    
    if not xml_path:
        print("\n" + "="*70)
        print("iTunes Library XML NOT FOUND")
        print("="*70)
        print("\nThe XML file is disabled by default in modern iTunes.")
        print("\nTo enable it:")
        print("  1. Open iTunes")
        print("  2. Go to: Edit → Preferences → Advanced")
        print("  3. Check: 'Share iTunes Library XML with other applications'")
        print("  4. Click OK")
        print("  5. Close and reopen iTunes")
        print("\nThis will create 'iTunes Library.xml' with your play counts.")
        print("\nSearched for:")
        print(f"  {ITUNES_LIBRARY_XML}")
        print(f"  {ITUNES_MUSIC_LIBRARY_XML}")
        print("\n" + "="*70)
        input("\nPress Enter to close...")
        exit(1)
    
    print(f"Loading iTunes Library from {xml_path}...")
    
    try:
        with open(xml_path, 'rb') as f:
            library = plistlib.load(f)
        
        print(f"✓ XML file loaded successfully")
        
        tracks = library.get('Tracks', {})
        print(f"✓ Found {len(tracks)} tracks in library")
        
        if len(tracks) == 0:
            print("\n⚠ WARNING: XML file has 0 tracks!")
            print("This might mean:")
            print("  • You just enabled XML sharing - try closing/reopening iTunes")
            print("  • Your library is actually empty")
            print("  • iTunes hasn't regenerated the XML file yet")
            input("\nPress Enter to close...")
            exit(1)
        
        # Debug: show sample track
        if tracks:
            first_track = next(iter(tracks.values()))
            print(f"\nDEBUG - Sample track:")
            print(f"  Artist: {first_track.get('Artist', 'N/A')}")
            print(f"  Name: {first_track.get('Name', 'N/A')}")
            print(f"  Play Count: {first_track.get('Play Count', 0)}")
            print(f"  Rating: {first_track.get('Rating', 0)}")
            location = first_track.get('Location', '')
            if location:
                print(f"  Location (first 100 chars): {location[:100]}...")
        
        # Build dictionary: filepath -> {plays, rating, artist, album}
        track_data = {}
        tracks_with_plays = 0
        tracks_with_rating = 0
        
        for track_id, track in tracks.items():
            location = track.get('Location', '')
            if not location:
                continue
            
            # Convert file:// URL to Windows path
            try:
                parsed = urlparse(location)
                filepath = unquote(parsed.path)
                # Remove leading slash for Windows paths
                if filepath.startswith('/') and ':' in filepath:
                    filepath = filepath[1:]
                
                # Normalize path separators
                filepath = filepath.replace('/', '\\')
                
                play_count = track.get('Play Count', 0)
                rating = track.get('Rating', 0)
                
                if play_count > 0:
                    tracks_with_plays += 1
                if rating > 0:
                    tracks_with_rating += 1
                
                track_data[filepath.lower()] = {
                    'plays': play_count,
                    'rating': rating,
                    'artist': track.get('Artist', ''),
                    'album': track.get('Album', '')
                }
            except Exception as e:
                continue
        
        print(f"\n✓ Loaded {len(track_data)} tracks from iTunes Library")
        print(f"  - Tracks with play count > 0: {tracks_with_plays}")
        print(f"  - Tracks with ratings > 0: {tracks_with_rating}\n")
        
        if tracks_with_plays == 0 and tracks_with_rating == 0:
            print("⚠ No tracks have play counts or ratings")
            print("  Albums will be sorted alphabetically")
        
        return track_data
    
    except Exception as e:
        print(f"✗ Error loading iTunes Library: {e}")
        import traceback
        traceback.print_exc()
        input("\nPress Enter to close...")
        exit(1)

def scan_library():
    """Scan music library and match with iTunes data"""
    
    # Load iTunes Library data
    itunes_data = load_itunes_library()
    
    # Dictionary to store albums
    albums = defaultdict(lambda: {'tracks': 0, 'plays': 0, 'ratings': []})
    
    matched_tracks = 0
    unmatched_tracks = 0
    
    for music_dir in MUSIC_DIRS:
        if not os.path.exists(music_dir):
            print(f"✗ Directory not found: {music_dir}")
            continue
        
        print(f"Scanning {music_dir}...\n")
        
        # Walk through directory structure
        for root, dirs, files in os.walk(music_dir):
            audio_files = [f for f in files if Path(f).suffix.lower() in AUDIO_EXTENSIONS]
            
            if audio_files:
                parts = Path(root).parts
                
                if len(parts) >= 2:
                    album = parts[-1]
                    artist = parts[-2]
                    
                    if artist == 'Apple Music':
                        continue
                    
                    key = (artist, album)
                    albums[key]['tracks'] += len(audio_files)
                    
                    # Match with iTunes Library data
                    for audio_file in audio_files:
                        filepath = os.path.join(root, audio_file)
                        filepath_lower = filepath.lower()
                        
                        if filepath_lower in itunes_data:
                            track = itunes_data[filepath_lower]
                            albums[key]['plays'] += track['plays']
                            if track['rating'] > 0:
                                albums[key]['ratings'].append(track['rating'])
                            matched_tracks += 1
                        else:
                            unmatched_tracks += 1
    
    print(f"\n✓ Scan complete")
    print(f"  - Matched tracks: {matched_tracks}")
    print(f"  - Unmatched tracks: {unmatched_tracks}")
    print(f"  - Total albums still in library: {len(albums)}")
    
    if matched_tracks == 0:
        print("\n⚠ No tracks matched between filesystem and iTunes Library!")
        print("\nThis usually means path format mismatch.")
        print("Showing sample paths from each source:\n")
        
        print("iTunes Library paths (first 3):")
        for i, path in enumerate(list(itunes_data.keys())[:3]):
            print(f"  {path}")
        
        print("\nFilesystem paths (first 3):")
        sample_count = 0
        for music_dir in MUSIC_DIRS:
            if os.path.exists(music_dir):
                for root, dirs, files in os.walk(music_dir):
                    for file in files:
                        if Path(file).suffix.lower() in AUDIO_EXTENSIONS:
                            print(f"  {os.path.join(root, file).lower()}")
                            sample_count += 1
                            if sample_count >= 3:
                                break
                    if sample_count >= 3:
                        break
        
        input("\nPress Enter to close...")
        return
    
    print("\n" + "="*70)
    print("DOWNLOAD PRIORITY LIST")
    print("="*70)
    print("Filtered to show ONLY albums you still need to download")
    print("Sorted by play count and ratings")
    print("="*70 + "\n")
    
    # Calculate priority scores - ONLY for albums in the needed list
    album_scores = []
    filtered_out = 0
    
    for (artist, album), data in albums.items():
        # Check if this album is in the needed list
        if (artist, album) not in ALBUMS_NEEDED:
            filtered_out += 1
            continue
        
        tracks = data['tracks']
        total_plays = data['plays']
        avg_rating = sum(data['ratings']) / len(data['ratings']) if data['ratings'] else 0
        
        plays_per_track = total_plays / tracks if tracks > 0 else 0
        priority_score = (plays_per_track * 10) + (avg_rating / 10)
        
        album_scores.append({
            'artist': artist,
            'album': album,
            'tracks': tracks,
            'total_plays': total_plays,
            'avg_rating': avg_rating,
            'score': priority_score
        })
    
    print(f"  - Albums filtered out (already downloaded): {filtered_out}")
    print(f"  - Albums remaining to download: {len(album_scores)}\n")
    
    # Sort and display
    if any(a['score'] > 0 for a in album_scores):
        sorted_albums = sorted(album_scores, key=lambda x: (-x['score'], x['artist'].lower(), x['album'].lower()))
        
        for idx, album in enumerate(sorted_albums, 1):
            priority = ""
            if album['score'] > 50:
                priority = " ⭐⭐⭐ HIGH PRIORITY"
            elif album['score'] > 20:
                priority = " ⭐⭐ MEDIUM PRIORITY"
            elif album['score'] > 5:
                priority = " ⭐ LOW PRIORITY"
            
            plays_info = f" - {album['total_plays']} plays" if album['total_plays'] > 0 else ""
            rating_info = f" - {album['avg_rating']:.0f}★" if album['avg_rating'] > 0 else ""
            
            print(f"{idx}. {album['artist']} - {album['album']} ({album['tracks']} tracks){plays_info}{rating_info}{priority}")
    else:
        sorted_albums = sorted(album_scores, key=lambda x: (x['artist'].lower(), x['album'].lower()))
        
        print("Albums listed alphabetically (no play count data):\n")
        
        for idx, album in enumerate(sorted_albums, 1):
            print(f"{idx}. {album['artist']} - {album['album']} ({album['tracks']} tracks)")

if __name__ == "__main__":
    import sys
    try:
        scan_library()
    except KeyboardInterrupt:
        print("\n\nScan interrupted", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        input("\nPress Enter to close...")