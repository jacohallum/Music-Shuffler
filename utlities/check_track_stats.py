"""
Track Statistics Diagnostic Tool
Check why a specific track keeps appearing at the top of shuffle
"""

import sys
import os
import time
import math
import random
import tkinter as tk
from tkinter import scrolledtext
from pathlib import Path

# Add parent directory to path to import from music_player
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
import plistlib

def load_track_metadata(filepath):
    """Load metadata from file and iTunes library"""
    metadata = {
        'title': os.path.basename(filepath),
        'artist': 'Unknown',
        'album': 'Unknown',
        'genre': '',
        'play_count': 0,
        'rating': 0,
        'last_played': None,
        'date_added': None,
        'loved': None,
        'skips': 0,
        'bpm': 0,
        'duration': 0
    }
    
    # Load from file tags
    try:
        audio = MutagenFile(filepath, easy=True)
        if audio:
            metadata['title'] = audio.get('title', [''])[0] or os.path.basename(filepath)
            metadata['artist'] = audio.get('artist', [''])[0] or 'Unknown'
            metadata['album'] = audio.get('album', [''])[0] or 'Unknown'
            metadata['genre'] = audio.get('genre', [''])[0] or ''
            
            if hasattr(audio.info, 'length'):
                metadata['duration'] = int(audio.info.length)
    except:
        pass
    
    # Load from iTunes library
    itunes_path = str(Path.home() / "Music" / "iTunes" / "iTunes Music Library.xml")
    if os.path.exists(itunes_path):
        try:
            with open(itunes_path, 'rb') as f:
                library = plistlib.load(f)
            
            tracks = library.get('Tracks', {})
            for track_id, track_data in tracks.items():
                location = track_data.get('Location', '')
                if location:
                    from urllib.parse import urlparse, unquote
                    parsed = urlparse(location)
                    file_path = unquote(parsed.path)
                    if file_path.startswith('/') and len(file_path) > 2 and file_path[2] == ':':
                        file_path = file_path[1:]
                    file_path = file_path.replace('/', '\\')
                    
                    if os.path.normcase(file_path) == os.path.normcase(filepath):
                        # Found it!
                        metadata['play_count'] = track_data.get('Play Count', 0)
                        metadata['rating'] = track_data.get('Rating', 0)
                        metadata['skips'] = track_data.get('Skip Count', 0)
                        metadata['loved'] = track_data.get('Loved', False)
                        
                        last_played = track_data.get('Play Date UTC') or track_data.get('Play Date')
                        if last_played:
                            if hasattr(last_played, 'timestamp'):
                                metadata['last_played'] = int(last_played.timestamp())
                            elif isinstance(last_played, (int, float)):
                                metadata['last_played'] = int(last_played)
                        
                        date_added = track_data.get('Date Added')
                        if date_added:
                            if hasattr(date_added, 'timestamp'):
                                metadata['date_added'] = int(date_added.timestamp())
                        
                        metadata['bpm'] = track_data.get('BPM', 0)
                        break
        except Exception as e:
            return None, f"Error loading iTunes library: {e}"
    
    return metadata, None

def calculate_weight(metadata):
    """Calculate Smart Shuffle weight exactly as the algorithm does"""
    now = time.time()
    
    # Helper functions (copied from CustomShuffleAlgorithm)
    def _norm_rating(r):
        if r is None:
            return 0.50
        try:
            r = int(r or 0)
        except:
            return 0.50
        if r == 0:
            return 0.50
        if r <= 5:
            return max(0.0, min(1.0, r / 5.0))
        if r <= 100:
            return max(0.0, min(1.0, r / 100.0))
        return max(0.0, min(1.0, r / 255.0))
    
    def _novelty_score(play_count):
        if play_count is None:
            return 0.60
        try:
            pc = int(play_count or 0)
        except:
            return 0.60
        
        # Key fix: shift counts so "never played" doesn't get a full 1.0 novelty.
        # pc=0 -> 1/(1+1)=0.5
        # pc=1 -> 1/(1+2)=0.333...
        pc_eff = max(pc, 0) + 1
        return 1.0 / (1.0 + pc_eff)
    
    def _recency_boost(last_played):
        if last_played is None:
            return 1.0
        try:
            lp = float(last_played)
        except:
            return 1.0
        if lp <= 0:
            return 1.0
        age_days = max(0.0, (now - lp) / 86400.0)
        half_life = 14.0
        return 1.0 - math.exp(-age_days / half_life)
    
    def _newness_boost(date_added):
        if date_added is None:
            return 0.20
        try:
            da = float(date_added)
        except:
            return 0.20
        if da <= 0:
            return 0.20
        age_days = max(0.0, (now - da) / 86400.0)
        if age_days <= 90:
            return 0.50
        return 0.0
    
    def _skip_penalty(skips):
        if skips is None:
            return 0.0
        try:
            s = int(skips or 0)
        except:
            return 0.0
        return 0.20 * (s ** 0.5) if s > 0 else 0.0
    
    def _love_score(loved):
        if loved is None:
            return 0.25
        return 1.0 if loved else 0.0
    
    # Calculate components
    random_component = 0.20 + random.random() * 0.50  # 0.20-0.70 range
    
    rating = _norm_rating(metadata['rating'])
    novelty = _novelty_score(metadata['play_count'])
    rec = _recency_boost(metadata['last_played'])
    newness = _newness_boost(metadata['date_added'])
    loved = _love_score(metadata['loved'])
    skip_pen = _skip_penalty(metadata['skips'])
    
    weight = random_component
    weight += 0.55 * rating
    weight += 0.55 * novelty
    weight += 0.45 * rec
    weight += 0.20 * newness
    weight += 0.30 * loved
    weight -= skip_pen
    
    return {
        'total_weight': weight,
        'random': random_component,
        'rating_contribution': 0.55 * rating,
        'novelty_contribution': 0.55 * novelty,
        'recency_contribution': 0.45 * rec,
        'newness_contribution': 0.20 * newness,
        'love_contribution': 0.30 * loved,
        'skip_penalty': skip_pen,
        'components': {
            'rating_normalized': rating,
            'novelty_score': novelty,
            'recency_boost': rec,
            'newness_boost': newness,
            'love_score': loved
        }
    }

def format_time(epoch):
    """Format epoch timestamp"""
    if not epoch:
        return "Never"
    try:
        from datetime import datetime
        dt = datetime.fromtimestamp(epoch)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return str(epoch)

def format_duration(seconds):
    """Format duration in seconds to MM:SS"""
    if not seconds:
        return "0:00"
    mins = seconds // 60
    secs = seconds % 60
    return f"{mins}:{secs:02d}"

def generate_report(filepath):
    """Generate diagnostic report text"""
    output = []
    
    output.append("="*70)
    output.append("TRACK STATISTICS DIAGNOSTIC (UPDATED)")
    output.append("="*70)
    output.append(f"File: {filepath}")
    output.append("")
    
    if not os.path.exists(filepath):
        output.append("❌ FILE NOT FOUND!")
        return "\n".join(output)
    
    metadata, error = load_track_metadata(filepath)
    if error:
        output.append(f"❌ {error}")
        return "\n".join(output)
    
    output.append("📊 METADATA:")
    output.append("-"*70)
    output.append(f"  Title:       {metadata['title']}")
    output.append(f"  Artist:      {metadata['artist']}")
    output.append(f"  Album:       {metadata['album']}")
    output.append(f"  Genre:       {metadata['genre'] or '(none)'}")
    output.append(f"  Duration:    {format_duration(metadata['duration'])}")
    output.append("")
    
    output.append("🎵 PLAYBACK STATS:")
    output.append("-"*70)
    output.append(f"  Play Count:  {metadata['play_count']}")
    output.append(f"  Skip Count:  {metadata['skips']}")
    output.append(f"  Rating:      {metadata['rating']} / 100")
    output.append(f"  Loved:       {metadata['loved']}")
    output.append(f"  BPM:         {metadata['bpm']}")
    output.append(f"  Last Played: {format_time(metadata['last_played'])}")
    output.append(f"  Date Added:  {format_time(metadata['date_added'])}")
    output.append("")
    
    # Calculate age
    now = time.time()
    if metadata['last_played']:
        age_days = (now - metadata['last_played']) / 86400.0
        output.append(f"  🕒 Last played {age_days:.1f} days ago")
    if metadata['date_added']:
        added_days = (now - metadata['date_added']) / 86400.0
        output.append(f"  📅 Added {added_days:.0f} days ago")
    output.append("")
    
    output.append("⚖️  SMART SHUFFLE WEIGHT CALCULATION (NEW FORMULA):")
    output.append("-"*70)
    
    weight_info = calculate_weight(metadata)
    
    output.append(f"  Total Weight:        {weight_info['total_weight']:.4f}")
    output.append("")
    output.append("  Components:")
    output.append(f"    Random:            {weight_info['random']:.4f} (0.20-0.70 range)")
    output.append(f"    Rating:            {weight_info['rating_contribution']:.4f} (0.55 × {weight_info['components']['rating_normalized']:.2f})")
    output.append(f"    Novelty:           {weight_info['novelty_contribution']:.4f} (0.55 × {weight_info['components']['novelty_score']:.2f})")
    output.append(f"    Recency:           {weight_info['recency_contribution']:.4f} (0.45 × {weight_info['components']['recency_boost']:.2f})")
    output.append(f"    Newness:           {weight_info['newness_contribution']:.4f} (0.20 × {weight_info['components']['newness_boost']:.2f})")
    output.append(f"    Love:              {weight_info['love_contribution']:.4f} (0.30 × {weight_info['components']['love_score']:.2f})")
    output.append(f"    Skip Penalty:     -{weight_info['skip_penalty']:.4f}")
    output.append("")
    
    output.append("🔍 DIAGNOSIS:")
    output.append("-"*70)
    
    # Identify what's causing high score
    issues = []
    
    if weight_info['novelty_contribution'] > 0.40:
        issues.append(f"✓ High novelty score ({weight_info['novelty_contribution']:.2f}) - Play count: {metadata['play_count']}")
    
    if weight_info['recency_contribution'] > 0.40:
        issues.append(f"✓ High recency boost ({weight_info['recency_contribution']:.2f}) - Not played recently or never")
    
    if weight_info['rating_contribution'] > 0.40:
        issues.append(f"✓ High rating ({metadata['rating']}/100)")
    
    if metadata['loved']:
        issues.append(f"✓ Loved track (+0.30)")
    
    if weight_info['newness_contribution'] > 0.15:
        issues.append(f"✓ Recently added track (+{weight_info['newness_contribution']:.2f})")
    
    if not issues:
        issues.append("No obvious high-scoring factors - likely random variation")
    
    for issue in issues:
        output.append(f"  {issue}")
    
    output.append("")
    output.append("="*70)
    
    return "\n".join(output)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python check_track_stats.py <path_to_audio_file>")
        sys.exit(1)
    filepath = sys.argv[1]

    # Create window
    root = tk.Tk()
    root.title("Track Statistics Diagnostic (UPDATED)")
    root.geometry("900x700")

    # Create scrolled text widget
    text_widget = scrolledtext.ScrolledText(root, wrap=tk.WORD, font=("Consolas", 10))
    text_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    # Generate and display report
    report = generate_report(filepath)
    text_widget.insert(tk.END, report)
    text_widget.config(state=tk.DISABLED)  # Make read-only

    # Run GUI
    root.mainloop()
