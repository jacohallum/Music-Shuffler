"""
Custom Shuffle Music Player V2 - Unified Metadata Edition

V2 CHANGES (2025-01):
- Unified metadata system: All 8,812 tracks tracked equally
- Single XML file: music_player_metadata.xml (no iTunes dependency)
- Fresh start: Build metadata from actual usage
- Simpler codebase: Removed ~200 lines of dual-system complexity
- All tracks: play_count, skip_count, rating, loved, last_played, date_added

FEATURES:
- Smart Shuffle v2 with novelty curve fix (0-play tracks = 0.5, not 1.0)
- Background .m4a pre-conversion (iTunes Media folder)
- Global media keys (F5-F8)
- Search & filter (8K+ library)
- Up Next queue system
- Album artwork display
- Path-based metadata parsing for M4P Downloads FLACs
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import os
import random
import re
import queue
from collections import deque
from shuffle_core import CustomShuffleAlgorithm
from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.flac import FLAC

# Suppress pygame pkg_resources deprecation warning
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pkg_resources")

import pygame
import threading
import time
from datetime import datetime
import sys
import json
from pathlib import Path
import subprocess  # For calling FFmpeg
import hashlib  # For creating unique cache filenames
from PIL import Image, ImageTk  # For album artwork
import io
import plistlib  # For parsing iTunes XML library
from urllib.parse import unquote, urlparse  # For decoding iTunes file URLs


# =============================================================================
# Constants
# =============================================================================
SKIP_COMPLETION_THRESHOLD = 0.80  # Track must be <80% complete to count as skip
MIN_DISK_SPACE_MB = 50  # Minimum disk space for cache operations
MIN_DISK_SPACE_PRECONV_MB = 500  # Minimum disk space for pre-conversion
MIN_VALID_CACHE_SIZE = 1024  # Minimum bytes for valid converted file
END_DETECTION_BUFFER_SEC = 2.0  # Buffer seconds for track end detection
VOLUME_SYNC_INTERVAL_SEC = 2.0  # How often to sync volume slider with system
METADATA_SAVE_DEBOUNCE_SEC = 0.1  # Debounce delay for metadata saves
SEARCH_DEBOUNCE_MS = 100  # Debounce delay for search filtering
ARTWORK_SIZE = 500  # Album artwork thumbnail size in pixels


class MusicTrack:
    """Represents a music track with all its metadata"""
    
    def __init__(self, filepath, meta_path=None, player_metadata=None):
        self.filepath = filepath  # Used for playback
        self.meta_path = meta_path or filepath  # Used for metadata (original or decrypted)
        self.filename = os.path.basename(self.meta_path)
        self.original_filepath = filepath  # Always initialize - prevents crashes with old cache
        
        # Default values
        self.title = self.filename
        self.artist = "Unknown Artist"
        self.album = "Unknown Album"
        self.genre = ""
        self.year = ""
        self.duration = 0
        self.rating = 0
        self.play_count = 0
        self.bpm = 0
        self.track_number = None  # Track number within album
        
        # Optional metadata (only if stored in-file). None = unknown.
        self.loved = None        # bool|None
        self.last_played = None  # epoch seconds|None
        self.skips = None        # int|None
        self.date_added = None   # epoch seconds|None
        
        self._load_metadata()
        
        # Fallback: Parse path/filename for M4P Downloads files with missing tags
        if "m4p downloads" in self.filepath.lower():
            self._parse_m4p_path_metadata()
        
        # V2: Apply player metadata if provided (takes precedence over file metadata)
        if player_metadata:
            self._apply_player_metadata(player_metadata)
    
    def _apply_player_metadata(self, metadata):
        """Apply metadata from player metadata file"""
        self.play_count = metadata.get('play_count', self.play_count)
        self.skips = metadata.get('skip_count', self.skips)
        self.rating = metadata.get('rating', self.rating)
        self.bpm = metadata.get('bpm', self.bpm)
        
        # Last played - handle datetime object or epoch
        last_played = metadata.get('last_played')
        if last_played:
            if hasattr(last_played, 'timestamp'):
                self.last_played = int(last_played.timestamp())
            elif isinstance(last_played, (int, float)):
                self.last_played = int(last_played)
        
        # Date added - handle datetime object or epoch
        date_added = metadata.get('date_added')
        if date_added:
            if hasattr(date_added, 'timestamp'):
                self.date_added = int(date_added.timestamp())
            elif isinstance(date_added, (int, float)):
                self.date_added = int(date_added)
        
        # Loved status
        self.loved = metadata.get('loved', self.loved)
    
    def _load_metadata(self):
        """Load metadata from the meta_path"""
        try:
            audio = MutagenFile(self.meta_path, easy=True)
            
            if audio is None:
                return
            
            # Common tags across formats
            self.title = self._get_tag(audio, 'title') or self.filename
            self.artist = self._get_tag(audio, 'artist') or "Unknown Artist"
            self.album = self._get_tag(audio, 'album') or "Unknown Album"
            self.genre = self._get_tag(audio, 'genre') or ""
            self.year = self._get_tag(audio, 'date') or ""
            
            # Track number (may be "5" or "5/12" format)
            track_num = self._get_tag(audio, 'tracknumber')
            if track_num:
                self.track_number = track_num  # Keep original format (string)
            
            # Get duration
            if hasattr(audio.info, 'length'):
                self.duration = int(audio.info.length)
            
            # Try to get additional metadata from full tags
            try:
                full_audio = MutagenFile(self.meta_path)
                
                # MP3 specific tags
                if isinstance(full_audio, MP3):
                    # Rating (POPM frame)
                    if 'POPM:Windows Media Player 9 Series' in full_audio:
                        rating_data = full_audio['POPM:Windows Media Player 9 Series']
                        self.rating = rating_data.rating
                    
                    # Play count
                    if 'PCNT' in full_audio:
                        self.play_count = int(full_audio['PCNT'].count)
                    
                    # BPM
                    if 'TBPM' in full_audio:
                        try:
                            self.bpm = int(str(full_audio['TBPM']))
                        except:
                            pass
                    
                    # Additional: scan custom text frames for love/last played/skips/date added
                    try:
                        if full_audio.tags:
                            for key, frame in full_audio.tags.items():
                                if not key.startswith("TXXX"):
                                    continue

                                try:
                                    desc = (getattr(frame, "desc", "") or "").strip().lower()
                                    texts = getattr(frame, "text", []) or []
                                    val = self._to_text(texts[0]) if texts else ""
                                    val_l = val.strip().lower()
                                except (AttributeError, IndexError, TypeError):
                                    continue

                                # Loved
                                if self.loved is None and ("love" in desc or "loved" in desc):
                                    self.loved = val_l in ("1", "true", "yes", "y", "on")

                                # Last played
                                if self.last_played is None and ("last played" in desc or "lastplayed" in desc):
                                    self.last_played = self._parse_epoch(val, default=None)

                                # Skips
                                if self.skips is None and ("skip" in desc or "skipped" in desc):
                                    self.skips = self._parse_int(val, default=None)

                                # Date added
                                if self.date_added is None and ("date added" in desc or "dateadded" in desc or desc == "added"):
                                    self.date_added = self._parse_epoch(val, default=None)
                    except:
                        pass
                
                # MP4/M4A specific tags
                elif isinstance(full_audio, MP4):
                    # Rating
                    if '----:com.apple.iTunes:rating' in full_audio:
                        self.rating = int(full_audio['----:com.apple.iTunes:rating'][0])
                    
                    # Play count
                    if '----:com.apple.iTunes:play count' in full_audio:
                        self.play_count = int(full_audio['----:com.apple.iTunes:play count'][0])
                    
                    # BPM
                    if 'tmpo' in full_audio:
                        self.bpm = int(full_audio['tmpo'][0])
                    
                    # Additional: scan tags (including freeform atoms) heuristically
                    try:
                        tags = full_audio.tags or {}
                        for k, v in tags.items():
                            ktxt = self._to_text(k).strip().lower()

                            # MP4 tags often store lists
                            val0 = v[0] if isinstance(v, list) and v else v
                            vtxt = self._to_text(val0).strip()
                            vtxt_l = vtxt.lower()

                            # Loved
                            if self.loved is None and ("love" in ktxt or "loved" in ktxt):
                                self.loved = vtxt_l in ("1", "true", "yes", "y", "on")

                            # Last played
                            if self.last_played is None and ("last played" in ktxt or "lastplayed" in ktxt):
                                self.last_played = self._parse_epoch(vtxt, default=None)

                            # Skips
                            if self.skips is None and ("skip" in ktxt or "skipped" in ktxt):
                                self.skips = self._parse_int(vtxt, default=None)

                            # Date added
                            if self.date_added is None and ("date added" in ktxt or "dateadded" in ktxt):
                                self.date_added = self._parse_epoch(vtxt, default=None)
                    except:
                        pass
                
                # FLAC specific tags (Vorbis comments)
                elif isinstance(full_audio, FLAC):
                    # FLAC uses simple key-value Vorbis comments
                    try:
                        # Play count
                        if 'PLAYCOUNT' in full_audio:
                            self.play_count = self._parse_int(full_audio['PLAYCOUNT'][0], default=0)
                        
                        # Last played
                        if 'LASTPLAYED' in full_audio:
                            self.last_played = self._parse_epoch(full_audio['LASTPLAYED'][0], default=None)
                        
                        # Skip count
                        if 'SKIPCOUNT' in full_audio:
                            self.skips = self._parse_int(full_audio['SKIPCOUNT'][0], default=None)
                        
                        # Loved
                        if 'LOVED' in full_audio:
                            loved_val = self._to_text(full_audio['LOVED'][0]).strip()
                            self.loved = loved_val == '1'
                    except:
                        pass
            except:
                pass
                
        except Exception as e:
            # Suppress FLAC validation errors for corrupted/mislabeled files
            error_msg = str(e).lower()
            if "not a valid flac file" in error_msg:
                # Silently skip - these files will still load with basic filename metadata
                pass
            else:
                print(f"Error loading metadata for {self.meta_path}: {e}")
    
    def _parse_m4p_path_metadata(self):
        r"""
        Parse artist/album from M4P Downloads path structure when tags are missing.
        Expected structure: M4P Downloads\{Artist}\{Album}\{Artist} - {Album} - {Track} {Title}.ext
        Example: M4P Downloads\Future\FUTURE\Future - FUTURE - 01-10 Scrape.flac
        """
        try:
            # Only parse if tags are actually missing
            if self.artist != "Unknown Artist" and self.album != "Unknown Album":
                return
            
            # Split path into parts (cross-platform)
            # Note: Path is already imported at module level
            parts = Path(self.filepath).parts
            
            # Find "M4P Downloads" in the path
            try:
                m4p_idx = next(i for i, p in enumerate(parts) if p.lower() == "m4p downloads")
            except StopIteration:
                return
            
            # Extract artist and album from directory structure
            # Structure: M4P Downloads\{Artist}\{Album}\filename
            if len(parts) > m4p_idx + 2:
                path_artist = parts[m4p_idx + 1].strip()
                path_album = parts[m4p_idx + 2].strip()
                
                # Only use if not empty
                if path_artist and self.artist == "Unknown Artist":
                    self.artist = path_artist
                if path_album and self.album == "Unknown Album":
                    self.album = path_album
            
            # Try to parse filename: {Artist} - {Album} - {Track} {Title}
            filename_no_ext = os.path.splitext(self.filename)[0]
            parts_filename = filename_no_ext.split(' - ')
            
            if len(parts_filename) >= 3:
                # Format: Artist - Album - Track Title
                file_artist = parts_filename[0].strip()
                file_album = parts_filename[1].strip()
                
                # Prefer filename over directory if both available
                if file_artist and self.artist == "Unknown Artist":
                    self.artist = file_artist
                if file_album and self.album == "Unknown Album":
                    self.album = file_album
                
                # Extract track number and title from remaining parts
                track_title = parts_filename[2].strip()
                if self.title == self.filename:  # Only if we don't have a better title
                    # Remove track number prefix (e.g., "01-10 Scrape" -> "Scrape")
                    title_match = re.match(r'^[\d\-]+\s+(.+)', track_title)
                    if title_match:
                        self.title = title_match.group(1).strip()
                    else:
                        self.title = track_title
        except Exception as e:
            # Silently fail - we still have the filename as fallback
            pass
    
    def _get_tag(self, audio, tag_name):
        """Safely get a tag value"""
        try:
            value = audio.get(tag_name, [''])[0]
            return str(value) if value else ''
        except (KeyError, IndexError, TypeError):
            return ''
    
    def _to_text(self, v):
        """Best-effort convert tag value to a plain string."""
        try:
            if v is None:
                return ""
            if isinstance(v, (bytes, bytearray)):
                return v.decode("utf-8", errors="ignore")
            return str(v)
        except (AttributeError, TypeError, UnicodeDecodeError):
            return ""

    def _parse_int(self, s, default=None):
        """Parse int. Returns default (None recommended) if not parseable."""
        try:
            return int(float(str(s).strip()))
        except (ValueError, TypeError, AttributeError):
            return default

    def _parse_epoch(self, s, default=None):
        """
        Best-effort epoch parser:
          - epoch seconds (int/float)
          - epoch millis
          - ISO-ish timestamps (rare)
        Returns default (None recommended) on failure.
        """
        txt = str(s).strip() if s is not None else ""
        if not txt:
            return default

        # epoch seconds/millis
        try:
            n = float(txt)
            if n > 10_000_000_000:  # likely ms
                n = n / 1000.0
            return int(n)
        except (ValueError, TypeError):
            pass

        # ISO-ish timestamp (rare)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                # keep minimal; clamp length
                return int(datetime.strptime(txt[:19], fmt).timestamp())
            except ValueError:
                continue

        return default
    
    def format_duration(self):
        """Format duration as MM:SS"""
        minutes = int(self.duration // 60)
        seconds = int(self.duration % 60)
        return f"{minutes}:{seconds:02d}"

    def to_dict(self):
        """Serialize track to JSON-safe dictionary"""
        return {
            'filepath': self.filepath,
            'meta_path': self.meta_path,
            'original_filepath': getattr(self, 'original_filepath', self.filepath),
            'filename': self.filename,
            'title': self.title,
            'artist': self.artist,
            'album': self.album,
            'genre': self.genre,
            'year': self.year,
            'duration': self.duration,
            'rating': self.rating,
            'play_count': self.play_count,
            'bpm': self.bpm,
            'track_number': self.track_number,
            'loved': self.loved,
            'last_played': self.last_played,
            'skips': self.skips,
            'date_added': self.date_added,
        }

    @classmethod
    def from_dict(cls, data, player_metadata=None):
        """Deserialize track from dictionary (skips file I/O)"""
        # Create instance without calling __init__ to avoid file I/O
        track = cls.__new__(cls)
        track.filepath = data.get('filepath', '')
        track.meta_path = data.get('meta_path', track.filepath)
        track.original_filepath = data.get('original_filepath', track.filepath)
        track.filename = data.get('filename', os.path.basename(track.meta_path))
        track.title = data.get('title', track.filename)
        track.artist = data.get('artist', 'Unknown Artist')
        track.album = data.get('album', 'Unknown Album')
        track.genre = data.get('genre', '')
        track.year = data.get('year', '')
        track.duration = data.get('duration', 0)
        track.rating = data.get('rating', 0)
        track.play_count = data.get('play_count', 0)
        track.bpm = data.get('bpm', 0)
        track.track_number = data.get('track_number')
        track.loved = data.get('loved')
        track.last_played = data.get('last_played')
        track.skips = data.get('skips')
        track.date_added = data.get('date_added')

        # Apply player metadata if provided (takes precedence)
        if player_metadata:
            track._apply_player_metadata(player_metadata)

        return track

    def __repr__(self):
        return f"<Track: {self.artist} - {self.title}>"


class MusicPlayerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Music Player V2")
        self.root.geometry("550x750")
        self.root.minsize(550, 650)
        self.root.resizable(False, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # Music library
        self.library_tracks = []       # Full scanned library (source of truth)
        self.all_tracks = []           # Active working subset (filtered view)
        self.current_playlist = []
        self.filtered_playlist = []    # For search filtering
        self._search_job = None        # For search debouncing
        self.current_index = 0

        # Recent play history for shuffle history guard (Task 5)
        # Stores (normalized_filepath, artist_key_or_None) tuples
        self._recent_play_history = deque(maxlen=200)

        # Debug flag: set MUSIC_SHUFFLER_DEBUG_SCAN=1 to enable scan spam (Task 8)
        self.debug_scan_logging = os.getenv('MUSIC_SHUFFLER_DEBUG_SCAN', '0') == '1'

        # Thread locks for shared mutable state
        self._metadata_lock = threading.Lock()  # Protects self.player_metadata
        self._tracks_lock = threading.Lock()  # Protects self.all_tracks during scan

        # Up Next queue
        self.up_next = deque()
        self.up_next_set = set()  # Filepath deduplication
        
        # Library stats
        self.total_tracks = 0
        self.total_artists = 0
        self.total_albums = 0
        
        # Theme settings
        self.dark_mode = True
        self.themes = {
            'dark': {
                'bg': '#1e1e1e',
                'fg': '#ffffff',
                'secondary_fg': '#b0b0b0',
                'frame_bg': '#2d2d2d',
                'button_bg': '#3d3d3d',
                'placeholder': '#404040'
            },
            'light': {
                'bg': '#ffffff',
                'fg': '#000000',
                'secondary_fg': '#666666',
                'frame_bg': '#f0f0f0',
                'button_bg': '#e0e0e0',
                'placeholder': '#d0d0d0'
            }
        }
        
        # Music directories - load from local config or use sensible defaults
        self.music_dirs = self._load_music_dirs()
        
        # V2: Unified metadata system (no more iTunes XML dependency)
        self.player_metadata = {}  # Will hold normalized_filepath -> metadata mapping
        
        # Cache file location - detect if running as exe or script
        try:
            # Check if running as PyInstaller exe
            if getattr(sys, 'frozen', False):
                # Running as compiled exe - use exe location
                self.script_dir = Path(sys.executable).parent
                print(f"Running as EXE from: {self.script_dir}")
            else:
                # Running as script - use script location
                self.script_dir = Path(__file__).parent
                print(f"Running as script from: {self.script_dir}")
        except (NameError, AttributeError, OSError):
            self.script_dir = Path.cwd()
            print(f"Using current directory: {self.script_dir}")
        
        # First try: D:\iTunes Shuffler\music_shuffler_cache (next to exe/script)
        cache_dir = self.script_dir / "music_shuffler_cache"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, IOError) as e:
            print(f"⚠ Cannot create cache in script directory: {e}")
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            cache_dir = base / "CustomShuffleMusicPlayer" / "music_shuffler_cache"
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                print(f"Using fallback cache location: {cache_dir}")
            except (OSError, IOError) as e2:
                # Last resort: use temp directory with subdirectory
                import tempfile
                cache_dir = Path(tempfile.gettempdir()) / "CustomShuffleMusicPlayer" / "cache"
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    print(f"⚠ Using temporary cache location: {cache_dir}")
                    print(f"  Cache will be lost on system restart")
                except (OSError, IOError) as e3:
                    # Absolute last resort: show error and exit gracefully
                    error_msg = (
                        f"Cannot create cache directory anywhere:\n"
                        f"• Script dir: {self.script_dir / 'music_shuffler_cache'}\n"
                        f"• AppData: {base / 'CustomShuffleMusicPlayer'}\n"
                        f"• Temp: {Path(tempfile.gettempdir()) / 'CustomShuffleMusicPlayer'}\n\n"
                        f"The application cannot run without a cache directory.\n"
                        f"Please check your file system permissions."
                    )
                    messagebox.showerror("Fatal Error", error_msg)
                    sys.exit(1)
        
        self.cache_file = cache_dir / "library_cache.json"
        self.volume_pref_file = cache_dir / "volume_pref.txt"
        self.player_metadata_file = cache_dir / "music_player_metadata.xml"
        self.shuffle_config_file = cache_dir / "shuffle_config.json"
        print(f"Cache directory: {cache_dir}")
        
        # Initialize shuffle configuration
        self._init_shuffle_config()
        
        # System volume control (Windows)
        self._init_system_volume_control()
        
        # Convert cache directory
        self.convert_cache_dir = cache_dir / "convert_cache"
        try:
            self.convert_cache_dir.mkdir(parents=True, exist_ok=True)
            
            # Cleanup orphaned temp files from crashed conversions
            # Format: {hash}.{pid}.{thread_id}.mp3
            # Only delete files older than 5 minutes to avoid interfering with other instances
            import time
            current_time = time.time()
            for temp_file in self.convert_cache_dir.glob("*.*.*.mp3"):
                try:
                    # Check if file is stale (older than 5 minutes)
                    if (current_time - temp_file.stat().st_mtime) > 300:
                        temp_file.unlink()
                        print(f"Cleaned up stale temp file: {temp_file.name}")
                except Exception:
                    # Specific exception - file might be in use or deleted by another instance
                    pass
        except:
            pass
        
        # Locate FFmpeg
        self.ffmpeg_path = self.locate_ffmpeg()
        
        # Background pre-conversion (m4a -> mp3 cache)
        self._preconv_q = queue.Queue()
        self._preconv_stop = threading.Event()
        self._preconv_thread = None
        self._preconv_token = 0  # Token-based cancellation
        self._preconv_total = 0
        self._preconv_done = 0
        self._preconv_failed = 0
        
        # Metadata save worker (single background thread to avoid thread spam)
        self._metadata_save_queue = queue.Queue()
        self._metadata_save_stop = threading.Event()
        self._metadata_save_thread = None
        
        # Playback state
        self.is_playing = False
        self.current_position = 0
        self.audio_available = False
        
        # Playback position tracking
        self._seek_base_sec = 0.0
        self._segment_start_mono = None
        
        # Initialize pygame mixer for audio playback
        try:
            pygame.mixer.init()
            self.audio_available = True
        except Exception as e:
            print(f"⚠ Audio initialization failed: {e}")
            messagebox.showerror(
                "Audio Unavailable",
                f"Could not initialize audio device:\n{e}\n\n"
                "The app will continue but playback will be disabled.\n\n"
                "Common causes:\n"
                "• No audio device available\n"
                "• Audio device in exclusive mode\n"
                "• Running over Remote Desktop"
            )
        
        self.setup_ui()
        self.setup_keyboard_shortcuts()
        self._set_dark_title_bar()
        self.check_ffmpeg()
        
        # V2: Load unified player metadata
        self._load_player_metadata()
        
        # Auto-load library on startup
        self.root.after(100, self.scan_library)
    
    def _load_music_dirs(self):
        """Load music directories from config.local.json, falling back to platform defaults."""
        config_path = Path(__file__).parent / "config.local.json" if not getattr(sys, 'frozen', False) else Path(sys.executable).parent / "config.local.json"
        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    cfg = json.load(f)
                dirs = cfg.get("music_dirs", [])
                if dirs:
                    return dirs
            except (json.JSONDecodeError, OSError) as e:
                print(f"Warning: could not read {config_path}: {e}")
        # Platform-aware defaults
        home = Path.home()
        defaults = [
            str(home / "Music" / "iTunes" / "iTunes Media" / "Music"),
            str(home / "Music" / "M4P Downloads"),
        ]
        return defaults

    def setup_ui(self):
        # Custom menu bar with search (replaces native menubar)
        menubar_frame = ttk.Frame(self.root, padding=(10, 6))
        menubar_frame.grid(row=0, column=0, sticky=(tk.W, tk.E))
        menubar_frame.columnconfigure(1, weight=1)  # Spacer between menus and search
        
        # Left side: Menu buttons
        menu_container = ttk.Frame(menubar_frame)
        menu_container.grid(row=0, column=0, sticky=tk.W)
        
        # Shuffle menu
        shuffle_btn = ttk.Menubutton(menu_container, text="Shuffle")
        shuffle_menu = tk.Menu(shuffle_btn, tearoff=0)
        shuffle_btn['menu'] = shuffle_menu
        shuffle_btn.grid(row=0, column=0, padx=(0, 5))
        
        shuffle_menu.add_command(label="Smart Shuffle", command=lambda: self.apply_shuffle("Smart Shuffle"))
        shuffle_menu.add_command(label="Truly Random", command=lambda: self.apply_shuffle("Truly Random"))
        
        # Settings menu
        settings_btn = ttk.Menubutton(menu_container, text="Settings")
        settings_menu = tk.Menu(settings_btn, tearoff=0)
        settings_btn['menu'] = settings_menu
        settings_btn.grid(row=0, column=1, padx=(0, 5))
        
        settings_menu.add_command(label="Library Info", command=self.show_library_info)
        settings_menu.add_separator()
        settings_menu.add_command(label="Shuffle Settings", command=self.edit_shuffle_settings)
        settings_menu.add_separator()
        settings_menu.add_command(label="Toggle Dark/Light Mode", command=self.toggle_theme)
        settings_menu.add_separator()
        settings_menu.add_command(label="Scan Library", command=self.scan_library)
        settings_menu.add_command(label="Force Full Rescan", command=self.force_rescan)
        settings_menu.add_command(label="Change Folders", command=self.change_folders)
        settings_menu.add_separator()
        settings_menu.add_command(label="Set FFmpeg Path", command=self.set_ffmpeg_path)
        settings_menu.add_command(label="Clear Cache", command=self.clear_cache)
        settings_menu.add_command(label="Clear Metadata (Reset All)", command=self.clear_metadata)
        settings_menu.add_separator()
        settings_menu.add_command(label="Show Library Diagnostics", command=self.show_diagnostics)
        settings_menu.add_command(label="Show Metadata", command=self.show_metadata)
        settings_menu.add_command(label="Check File Path", command=self.check_file_path)
        settings_menu.add_separator()
        settings_menu.add_command(label="Filter Library", command=self.filter_library)
        settings_menu.add_command(label="Reload Library", command=self.scan_library)
        settings_menu.add_separator()
        settings_menu.add_command(label="View Up Next Queue", command=self.view_up_next)
        settings_menu.add_command(label="Clear Up Next Queue", command=self.clear_up_next)
        
        # Help menu
        help_btn = ttk.Menubutton(menu_container, text="Help")
        help_menu = tk.Menu(help_btn, tearoff=0)
        help_btn['menu'] = help_menu
        help_btn.grid(row=0, column=2)
        
        help_menu.add_command(label="Keyboard Shortcuts", command=self.show_shortcuts)
        help_menu.add_command(label="Test Hotkeys", command=self.test_hotkeys)
        help_menu.add_command(label="About", command=self.show_about)
        
        # Right side: Search box
        search_frame = ttk.Frame(menubar_frame)
        search_frame.grid(row=0, column=2, sticky=tk.E)
        
        ttk.Label(search_frame, text="🔍").grid(row=0, column=0, padx=(0, 5))
        
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', lambda *args: self._schedule_filter())
        
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=28)
        self.search_entry.grid(row=0, column=1)
        
        self.clear_search_btn = ttk.Button(search_frame, text="✕", width=3, 
                                           command=self.clear_search)
        self.clear_search_btn.grid(row=0, column=2, padx=(5, 0))
        
        # Keyboard shortcuts for search
        self.search_entry.bind("<Escape>", lambda e: (self.clear_search(), "break"))
        self.search_entry.bind("<Return>", lambda e: self._play_first_result())
        self.root.bind("<Control-f>", lambda e: (self.search_entry.focus(), self.search_entry.select_range(0, tk.END)))
        
        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        main_frame.columnconfigure(0, weight=1)
        
        # Now playing section
        now_playing_frame = ttk.Frame(main_frame, padding="10")
        now_playing_frame.grid(row=0, column=0, sticky=(tk.W, tk.E))
        
        # Album artwork
        self.artwork_label = ttk.Label(now_playing_frame)
        self.artwork_label.pack(pady=(0, 6))
        self.current_artwork = None
        
        # Progress bar and time
        progress_frame = ttk.Frame(now_playing_frame)
        progress_frame.pack(fill=tk.X, padx=6, pady=(0, 10))
        
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(0, 4))
        self.progress_bar.bind('<Button-1>', self.on_progress_click)
        
        self.time_label = ttk.Label(progress_frame, text="0:00 / 0:00", font=('Arial', 8))
        self.time_label.pack()
        
        # Info block
        self.info_frame = ttk.Frame(now_playing_frame)
        self.info_frame.pack(pady=(6, 0), fill=tk.X)
        
        self.now_playing_label = ttk.Label(self.info_frame, text="Not Playing", 
                                          font=('Arial', 14, 'bold'))
        self.now_playing_label.pack()
        
        self.now_playing_artist = ttk.Label(self.info_frame, text="", 
                                           font=('Arial', 10))
        self.now_playing_artist.pack()
        
        # Transport controls
        self.transport_frame = ttk.Frame(now_playing_frame, padding="10")
        
        self.prev_button = ttk.Button(self.transport_frame, text="⏮", command=self.previous_track, width=6)
        self.prev_button.grid(row=0, column=0, padx=5)
        
        self.play_pause_button = ttk.Button(self.transport_frame, text="▶ / ⏸", command=self.toggle_play_pause, width=8)
        self.play_pause_button.grid(row=0, column=1, padx=5)
        
        self.stop_button = ttk.Button(self.transport_frame, text="⏹", command=self.stop, width=6)
        self.stop_button.grid(row=0, column=2, padx=5)
        
        self.next_button = ttk.Button(self.transport_frame, text="⏭", command=self.next_track, width=6)
        self.next_button.grid(row=0, column=3, padx=5)
        
        # Love button - using tk.Button for color support
        self.love_button = tk.Button(self.transport_frame, text="♡", command=self.toggle_loved, 
                                      width=3, font=('Arial', 14), foreground="gray")
        self.love_button.grid(row=0, column=4, padx=(15, 5))
        self.love_button.grid_remove()  # Hidden by default
        
        # Volume control
        ttk.Label(self.transport_frame, text="🔊", font=('Arial', 10)).grid(row=0, column=5, padx=(20, 5))
        
        # Get current system volume or fall back to saved preference
        current_volume = self.get_system_volume()
        if current_volume is None:
            # Fallback to saved pygame volume if system volume unavailable
            current_volume = 70
            try:
                if self.volume_pref_file.exists():
                    with open(self.volume_pref_file, 'r') as f:
                        current_volume = float(f.read().strip())
            except:
                pass
        
        self._updating_volume = False  # Flag to prevent callback loops
        self.volume_var = tk.DoubleVar(value=current_volume)
        self.volume_slider = ttk.Scale(self.transport_frame, from_=0, to=100, 
                                       variable=self.volume_var, 
                                       orient=tk.HORIZONTAL, length=180,
                                       command=self.on_volume_change)
        self.volume_slider.grid(row=0, column=6, padx=5)
        
        # Set initial volume
        if self.volume_interface:
            # System volume control available
            self.set_system_volume(current_volume)
        elif self.audio_available:
            # Fallback to pygame volume
            pygame.mixer.music.set_volume(current_volume / 100.0)
        
        self.transport_frame.place_forget()
        self._transport_hide_job = None
        
        # Playlist display
        playlist_frame = ttk.LabelFrame(main_frame, text="Playlist", padding="5")
        playlist_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(5, 10))
        playlist_frame.columnconfigure(0, weight=1)
        playlist_frame.rowconfigure(1, weight=1)  # Treeview is on row 1
        main_frame.rowconfigure(1, weight=1)
        
        columns = ('Artist', 'Title', 'Album', 'Duration')
        self.playlist_tree = ttk.Treeview(playlist_frame, columns=columns, 
                                         show='tree headings', height=5)
        
        self.playlist_tree.heading('#0', text='#')
        self.playlist_tree.heading('Artist', text='Artist')
        self.playlist_tree.heading('Title', text='Title')
        self.playlist_tree.heading('Album', text='Album')
        self.playlist_tree.heading('Duration', text='Duration')
        
        self.playlist_tree.column('#0', width=50, stretch=False)
        self.playlist_tree.column('Artist', width=120)
        self.playlist_tree.column('Title', width=180)
        self.playlist_tree.column('Album', width=120)
        self.playlist_tree.column('Duration', width=50, stretch=False)
        
        scrollbar = ttk.Scrollbar(playlist_frame, orient=tk.VERTICAL, 
                                 command=self.playlist_tree.yview)
        self.playlist_tree.configure(yscrollcommand=scrollbar.set)
        
        self.playlist_tree.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scrollbar.grid(row=1, column=1, sticky=(tk.N, tk.S))
        
        self.playlist_tree.bind('<Double-1>', self.on_track_double_click)
        
        # Up Next context menu
        self.playlist_menu = tk.Menu(self.root, tearoff=0)
        self.playlist_menu.add_command(label="Play Next", command=self._ctx_play_next)
        self.playlist_menu.add_command(label="Add to Up Next", command=self._ctx_add_up_next)
        
        # Right-click support (Windows and Mac)
        self.playlist_tree.bind("<Button-3>", self._on_playlist_right_click)  # Windows/Linux
        self.playlist_tree.bind("<Control-Button-1>", self._on_playlist_right_click)  # Mac
        
        # Status bar
        self.status_label = ttk.Label(main_frame, text="Ready", 
                                     font=('Arial', 8), foreground='gray')
        self.status_label.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=(5, 0))
        
        self.start_update_thread()
        
        # Hover bindings
        def bind_hover(widget):
            widget.bind("<Enter>", lambda e: self._show_transport())
            widget.bind("<Leave>", lambda e: self._schedule_hide_transport())
        
        bind_hover(self.info_frame)
        bind_hover(self.now_playing_label)
        bind_hover(self.now_playing_artist)
        bind_hover(self.transport_frame)
        for child in self.transport_frame.winfo_children():
            bind_hover(child)
        
        self.apply_theme()
    
    def apply_theme(self):
        """Apply current theme to all UI elements"""
        theme = self.themes['dark'] if self.dark_mode else self.themes['light']
        
        self.root.configure(bg=theme['bg'])
        
        style = ttk.Style()
        style.theme_use('default')
        
        style.configure('TFrame', background=theme['bg'])
        style.configure('TLabelframe', background=theme['bg'], foreground=theme['fg'])
        style.configure('TLabelframe.Label', background=theme['bg'], foreground=theme['fg'])
        style.configure('TLabel', background=theme['bg'], foreground=theme['fg'])
        style.configure('TButton', background=theme['button_bg'], foreground=theme['fg'])
        style.map('TButton', background=[('active', theme['frame_bg'])])
        
        style.configure('Treeview',
                       background=theme['frame_bg'],
                       foreground=theme['fg'],
                       fieldbackground=theme['frame_bg'])
        style.configure('Treeview.Heading',
                       background=theme['button_bg'],
                       foreground=theme['fg'])
        style.map('Treeview', background=[('selected', theme['button_bg'])])
        
        style.configure('TProgressbar',
                       background=theme['fg'],
                       troughcolor=theme['frame_bg'],
                       bordercolor=theme['button_bg'],
                       lightcolor=theme['fg'],
                       darkcolor=theme['fg'])
        
        self._show_placeholder_artwork()
        
        if hasattr(self, 'status_label'):
            self.status_label.configure(foreground=theme['secondary_fg'])
        
        if hasattr(self, 'time_label'):
            self.time_label.configure(foreground=theme['secondary_fg'])
    
    def toggle_theme(self):
        """Toggle between dark and light mode"""
        self.dark_mode = not self.dark_mode
        self.apply_theme()
        self._set_dark_title_bar()  # Update title bar to match theme
        mode = "Dark" if self.dark_mode else "Light"
        print(f"Switched to {mode} mode")
    
    
    
    def _init_shuffle_config(self):
        """Initialize shuffle configuration with defaults"""
        # Default shuffle parameters
        self.shuffle_config = {
            # Weight parameters
            'random_min': 0.20,
            'random_range': 0.50,
            'rating_weight': 0.55,
            'novelty_weight': 0.55,
            'recency_weight': 0.45,
            'newness_weight': 0.20,
            'loved_weight': 0.30,
            'skip_penalty_weight': 0.40,
            'artist_skip_weight': 0.10,
            'bpm_bonus': 0.05,

            # Constraint parameters
            'recent_artists': 3,
            'recent_albums': 2,
            'recent_genres': 2,
            'lookahead': 400,

            # Adaptive spacing (Task 4)
            'adaptive_constraints': True,

            # History guard (Task 5)
            'history_guard_size': 30,
            'history_track_penalty': 0.35,
            'history_artist_penalty': 0.15,
        }
        
        # Load saved configuration if it exists
        if self.shuffle_config_file.exists():
            try:
                import json
                with open(self.shuffle_config_file, 'r') as f:
                    saved_config = json.load(f)
                    self.shuffle_config.update(saved_config)
                print("✓ Loaded shuffle configuration")
            except Exception as e:
                print(f"⚠ Could not load shuffle config: {e}")
                print("  Using default settings")
    
    def _save_shuffle_config(self):
        """Save current shuffle configuration"""
        try:
            import json
            with open(self.shuffle_config_file, 'w') as f:
                json.dump(self.shuffle_config, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving shuffle config: {e}")
            return False
    
    def edit_shuffle_settings(self):
        """Edit shuffle algorithm parameters"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Shuffle Settings")
        dialog.geometry("550x650")
        dialog.resizable(False, False)
        
        dialog.transient(self.root)
        dialog.grab_set()
        
        main_frame = ttk.Frame(dialog, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(main_frame, text="Smart Shuffle Parameters", 
                font=('Arial', 14, 'bold')).pack(pady=(0, 15))
        
        ttk.Label(main_frame, text="Adjust how Smart Shuffle prioritizes tracks:", 
                font=('Arial', 9)).pack(pady=(0, 20))
        
        # Create a canvas with scrollbar for parameters
        canvas = tk.Canvas(main_frame, height=400)
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Dictionary to hold variable references
        vars_dict = {}
        
        # Weight parameters
        ttk.Label(scrollable_frame, text="Weight Parameters", 
                font=('Arial', 11, 'bold')).grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(5, 10))
        
        weight_params = [
            ('random_min', 'Random Minimum', 0.0, 1.0, 0.05, 'Base randomness level'),
            ('random_range', 'Random Range', 0.0, 1.0, 0.05, 'Additional randomness variation'),
            ('rating_weight', 'Rating Weight', 0.0, 2.0, 0.05, 'Importance of track ratings'),
            ('novelty_weight', 'Novelty Weight', 0.0, 2.0, 0.05, 'Favor less-played tracks'),
            ('recency_weight', 'Recency Weight', 0.0, 2.0, 0.05, 'Favor recently unplayed tracks'),
            ('newness_weight', 'Newness Weight', 0.0, 2.0, 0.05, 'Favor recently added tracks'),
            ('loved_weight', 'Loved Weight', 0.0, 2.0, 0.05, 'Bonus for loved tracks'),
            ('skip_penalty_weight', 'Skip Penalty', 0.0, 2.0, 0.05, 'Penalty for skipped tracks'),
            ('artist_skip_weight', 'Artist Skip Penalty', 0.0, 1.0, 0.05, 'Penalty for skipped artists'),
            ('bpm_bonus', 'BPM Bonus', 0.0, 0.5, 0.01, 'Bonus for tracks with BPM data'),
        ]
        
        row = 1
        for param, label, min_val, max_val, increment, tooltip in weight_params:
            ttk.Label(scrollable_frame, text=f"{label}:").grid(row=row, column=0, sticky=tk.W, padx=(10, 5), pady=3)
            
            var = tk.DoubleVar(value=self.shuffle_config.get(param, 0.0))
            vars_dict[param] = var
            
            spinbox = ttk.Spinbox(scrollable_frame, from_=min_val, to=max_val, 
                                increment=increment, textvariable=var, width=8)
            spinbox.grid(row=row, column=1, sticky=tk.W, padx=5, pady=3)
            
            ttk.Label(scrollable_frame, text=f"({tooltip})", 
                    font=('Arial', 8), foreground='gray').grid(row=row, column=2, sticky=tk.W, padx=5, pady=3)
            
            row += 1
        
        # Constraint parameters
        ttk.Label(scrollable_frame, text="Constraint Parameters", 
                font=('Arial', 11, 'bold')).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(15, 10))
        row += 1
        
        constraint_params = [
            ('recent_artists', 'Recent Artists', 1, 10, 1, 'Avoid repeating artists'),
            ('recent_albums', 'Recent Albums', 1, 10, 1, 'Avoid repeating albums'),
            ('recent_genres', 'Recent Genres', 1, 10, 1, 'Avoid repeating genres'),
            ('lookahead', 'Lookahead', 50, 1000, 50, 'How far ahead to search for swaps'),
        ]

        for param, label, min_val, max_val, increment, tooltip in constraint_params:
            ttk.Label(scrollable_frame, text=f"{label}:").grid(row=row, column=0, sticky=tk.W, padx=(10, 5), pady=3)

            var = tk.IntVar(value=self.shuffle_config.get(param, 0))
            vars_dict[param] = var

            spinbox = ttk.Spinbox(scrollable_frame, from_=min_val, to=max_val,
                                  increment=increment, textvariable=var, width=8)
            spinbox.grid(row=row, column=1, sticky=tk.W, padx=5, pady=3)

            ttk.Label(scrollable_frame, text=f"({tooltip})",
                      font=('Arial', 8), foreground='gray').grid(row=row, column=2, sticky=tk.W, padx=5, pady=3)
            row += 1

        # Adaptive & History parameters (Tasks 4, 5, 6)
        ttk.Label(scrollable_frame, text="Adaptive & History Parameters",
                  font=('Arial', 11, 'bold')).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(15, 10))
        row += 1

        # adaptive_constraints checkbox
        adapt_var = tk.BooleanVar(value=bool(self.shuffle_config.get('adaptive_constraints', True)))
        vars_dict['adaptive_constraints'] = adapt_var
        ttk.Checkbutton(scrollable_frame, text="Adaptive Constraints",
                        variable=adapt_var).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=(10, 5), pady=3)
        ttk.Label(scrollable_frame, text="(auto-scale spacing from library size)",
                  font=('Arial', 8), foreground='gray').grid(row=row, column=2, sticky=tk.W, padx=5, pady=3)
        row += 1

        history_params = [
            ('history_guard_size',    'History Guard Size',   5, 200, 5,    'Recent tracks checked for penalty'),
            ('history_track_penalty', 'Track Penalty',        0.0, 1.0, 0.05, 'Weight penalty for recently played tracks'),
            ('history_artist_penalty','Artist Penalty',       0.0, 1.0, 0.05, 'Weight penalty for recently played artists'),
        ]
        for param, label, min_val, max_val, increment, tooltip in history_params:
            ttk.Label(scrollable_frame, text=f"{label}:").grid(row=row, column=0, sticky=tk.W, padx=(10, 5), pady=3)
            if isinstance(self.shuffle_config.get(param, 0), int) or increment == 5:
                var = tk.IntVar(value=int(self.shuffle_config.get(param, 0)))
            else:
                var = tk.DoubleVar(value=self.shuffle_config.get(param, 0.0))
            vars_dict[param] = var
            ttk.Spinbox(scrollable_frame, from_=min_val, to=max_val,
                        increment=increment, textvariable=var, width=8).grid(
                row=row, column=1, sticky=tk.W, padx=5, pady=3)
            ttk.Label(scrollable_frame, text=f"({tooltip})",
                      font=('Arial', 8), foreground='gray').grid(row=row, column=2, sticky=tk.W, padx=5, pady=3)
            row += 1
        
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Buttons
        button_frame = ttk.Frame(dialog)
        button_frame.pack(pady=15)
        
        def save_settings():
            # Update config
            for param, var in vars_dict.items():
                self.shuffle_config[param] = var.get()
            
            # Save to file
            if self._save_shuffle_config():
                messagebox.showinfo("Saved", "Shuffle settings saved successfully!")
                dialog.destroy()
            else:
                messagebox.showerror("Error", "Failed to save shuffle settings")
        
        def reset_defaults():
            result = messagebox.askyesno("Reset to Defaults", 
                                        "Reset all shuffle parameters to default values?")
            if result:
                defaults = {
                    'random_min': 0.20,
                    'random_range': 0.50,
                    'rating_weight': 0.55,
                    'novelty_weight': 0.55,
                    'recency_weight': 0.45,
                    'newness_weight': 0.20,
                    'loved_weight': 0.30,
                    'skip_penalty_weight': 0.40,
                    'artist_skip_weight': 0.10,
                    'bpm_bonus': 0.05,
                    'recent_artists': 3,
                    'recent_albums': 2,
                    'recent_genres': 2,
                    'lookahead': 400,
                    'adaptive_constraints': True,
                    'history_guard_size': 30,
                    'history_track_penalty': 0.35,
                    'history_artist_penalty': 0.15,
                }
                for param, var in vars_dict.items():
                    var.set(defaults.get(param, 0))
    
        ttk.Button(button_frame, text="Save", command=save_settings, width=12).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Reset Defaults", command=reset_defaults, width=15).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=dialog.destroy, width=12).pack(side=tk.LEFT, padx=5)
    
    def _init_system_volume_control(self):
        """Initialize Windows system volume control"""
        self.system_volume = None
        self.volume_interface = None
        
        if sys.platform != 'win32':
            print("System volume control only available on Windows")
            return
        
        try:
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL, CoCreateInstance
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, IMMDeviceEnumerator
            from pycaw.constants import CLSID_MMDeviceEnumerator
            
            # Create device enumerator
            deviceEnumerator = CoCreateInstance(
                CLSID_MMDeviceEnumerator,
                IMMDeviceEnumerator,
                CLSCTX_ALL
            )
            
            # Get default audio endpoint (0 = eRender, 0 = eConsole)
            defaultDevice = deviceEnumerator.GetDefaultAudioEndpoint(0, 0)
            
            # Activate the volume interface
            interface = defaultDevice.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self.volume_interface = cast(interface, POINTER(IAudioEndpointVolume))
            
            print("✓ System volume control enabled")
                
        except ImportError:
            print("⚠ System volume control unavailable")
            print("  Install with: pip install pycaw comtypes")
        except Exception as e:
            print(f"⚠ Could not initialize system volume: {e}")
            print("  Volume slider will control pygame only")
    
    def get_system_volume(self):
        """Get current system volume (0-100)"""
        if not self.volume_interface:
            return None
        try:
            # Get volume as scalar (0.0 to 1.0)
            volume_scalar = self.volume_interface.GetMasterVolumeLevelScalar()
            return int(volume_scalar * 100)
        except (AttributeError, OSError):
            # COM interface may be disconnected
            return None
        except Exception as e:
            print(f"Unexpected error getting volume: {e}")
            return None
    
    def set_system_volume(self, volume_percent):
        """Set system volume (0-100)"""
        if not self.volume_interface:
            return False
        try:
            # Set volume as scalar (0.0 to 1.0)
            volume_scalar = max(0.0, min(1.0, volume_percent / 100.0))
            self.volume_interface.SetMasterVolumeLevelScalar(volume_scalar, None)
            return True
        except Exception as e:
            print(f"Error setting system volume: {e}")
            return False
    
    def volume_up(self):
        """Increase system volume by 5%"""
        current = self.get_system_volume()
        if current is not None:
            new_volume = min(100, current + 5)
            self.set_system_volume(new_volume)
            try:
                self._updating_volume = True
                self.volume_var.set(new_volume)
            finally:
                self._updating_volume = False
        else:
            print("⚠ System volume control not available")
            print("  Install: pip install pycaw comtypes")
    
    def volume_down(self):
        """Decrease system volume by 5%"""
        current = self.get_system_volume()
        if current is not None:
            new_volume = max(0, current - 5)
            self.set_system_volume(new_volume)
            try:
                self._updating_volume = True
                self.volume_var.set(new_volume)
            finally:
                self._updating_volume = False
        else:
            print("⚠ System volume control not available")
            print("  Install: pip install pycaw comtypes")
    
    def toggle_mute(self):
        """Toggle system mute"""
        if not self.volume_interface:
            print("⚠ System volume control not available")
            print("  Install: pip install pycaw comtypes")
            return
        try:
            is_muted = self.volume_interface.GetMute()
            self.volume_interface.SetMute(not is_muted, None)
        except Exception as e:
            print(f"Error toggling mute: {e}")
    
    def _sync_volume_slider(self):
        """Sync volume slider with system volume (called periodically)"""
        if not self.volume_interface:
            return
        try:
            current_volume = self.get_system_volume()
            if current_volume is not None:
                # Only update if significantly different (avoid slider jitter)
                slider_value = self.volume_var.get()
                if abs(current_volume - slider_value) > 1:
                    try:
                        self._updating_volume = True
                        self.volume_var.set(current_volume)
                    finally:
                        self._updating_volume = False
        except (AttributeError, TypeError, tk.TclError):
            # Volume interface disconnected or Tk destroyed
            pass
    
    def setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts for playback controls"""
        # F5-F8 global hotkeys handle playback - no need to block other keys
        self._setup_global_media_keys()
        
        print("Keyboard shortcuts enabled:")
        print("  Media keys control playback globally (works out of focus)")
    
    def _setup_global_media_keys(self):
        """Enable media keys even when the window is not focused"""
        try:
            import keyboard
        except ImportError:
            print("⚠ Global media keys disabled")
            print("  Install with: pip install keyboard")
            print("  Then restart the app")
            return

        def ui_call(fn):
            self.root.after(0, fn)

        hotkeys = [
            ('f2', self.volume_down, 'Volume Down'),
            ('f3', self.volume_up, 'Volume Up'),
            ('f4', self.toggle_mute, 'Mute Toggle'),
            ('f5', self.stop, 'Stop'),
            ('f6', self.previous_track, 'Previous'),
            ('f7', self.toggle_play_pause, 'Play/Pause'),
            ('f8', self.next_track, 'Next'),
        ]

        registered_keys = []
        failed_keys = []
        
        for key_name, fn, label in hotkeys:
            try:
                keyboard.add_hotkey(key_name, lambda fn=fn: ui_call(fn), suppress=True)
                registered_keys.append(f"{key_name.upper()} = {label}")
            except Exception as e:
                failed_keys.append(f"{key_name.upper()} ({e})")
        
        if registered_keys:
            print("✓ Global media keys enabled:")
            for key in registered_keys:
                print(f"  {key}")
            if not failed_keys:
                print("  Works globally, even when window not focused")
        
        if failed_keys:
            print("⚠ Some keys failed to register:")
            for key in failed_keys:
                print(f"  {key}")
            print("  Try running as administrator")
            print("  Or close other apps using these keys")
    
    def _set_dark_title_bar(self):
        """Set title bar color to match theme (Windows 10/11)"""
        try:
            self.root.update()  # Ensure window is created
            hwnd = self.root.winfo_id()
            
            import ctypes
            # 1 = dark, 0 = light
            use_dark = 1 if self.dark_mode else 0
            
            # Try Windows 11 attribute (20) first, then Windows 10 (19)
            for attribute in [20, 19]:
                try:
                    value = ctypes.c_int(use_dark)
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
                    )
                    break
                except (AttributeError, OSError):
                    continue  # Attribute not supported or API call failed
        except (AttributeError, ImportError):
            pass  # Not Windows or ctypes not available
        except Exception:
            # Don't crash for cosmetic features
            pass
    
    def _show_transport(self):
        """Show transport controls on hover"""
        if getattr(self, "_transport_hide_job", None):
            try:
                self.root.after_cancel(self._transport_hide_job)
            except:
                pass
            self._transport_hide_job = None
        
        if not self.transport_frame.winfo_ismapped():
            self.transport_frame.place(in_=self.info_frame, relx=0.5, rely=0.5, anchor="center")
    
    def _schedule_hide_transport(self):
        """Schedule hiding transport controls after small delay"""
        if getattr(self, "_transport_hide_job", None):
            return
        self._transport_hide_job = self.root.after(200, self._hide_transport)
    
    def _hide_transport(self):
        """Hide transport controls"""
        self._transport_hide_job = None
        if self.transport_frame.winfo_ismapped():
            self.transport_frame.place_forget()
    
    def _set_navigation_enabled(self, enabled):
        """Enable or disable navigation buttons"""
        state = tk.NORMAL if enabled else tk.DISABLED
        self.prev_button.config(state=state)
        self.next_button.config(state=state)
    
    def _validate_playlist_index(self):
        """Validate current playlist and index are in valid state"""
        if not self.current_playlist:
            return False
        if self.current_index < 0 or self.current_index >= len(self.current_playlist):
            return False
        return True
    
    def _get_safe_duration(self, track):
        """Get track duration with validation to prevent division by zero"""
        try:
            duration = float(getattr(track, 'duration', 0))
            return max(duration, 0.01)  # Minimum 0.01s to prevent division by zero
        except (ValueError, TypeError):
            return 0.01
    
    def _check_disk_space(self, required_mb=100):
        """
        Check if sufficient disk space is available
        
        Args:
            required_mb: Minimum MB required
            
        Returns:
            bool: True if sufficient space, False otherwise
        """
        try:
            import shutil
            stats = shutil.disk_usage(self.cache_file.parent)
            available_mb = stats.free / (1024 * 1024)
            
            if available_mb < required_mb:
                print(f"⚠ Low disk space: {available_mb:.0f}MB available, {required_mb}MB required")
                return False
            
            return True
        except Exception as e:
            print(f"⚠ Could not check disk space: {e}")
            return True  # Assume OK if we can't check
    
    def toggle_loved(self):
        """V2: Toggle loved status for ANY track"""
        if not self._validate_playlist_index():
            return
        
        track = self.current_playlist[self.current_index]
        
        # Toggle loved status
        track.loved = not track.loved if track.loved is not None else True
        
        # Update button - use unicode hearts that respond to foreground color
        if track.loved:
            self.love_button.config(text="♥", foreground="red", font=('Arial', 14, 'bold'))
        else:
            self.love_button.config(text="♡", foreground="gray", font=('Arial', 14))
        
        # V2: Save to unified metadata
        self._update_track_metadata(track)
        
        print(f"{'♥' if track.loved else '♡'} {track.title}")
    
    def _update_love_button(self, track):
        """V2: Update love button state (always visible)"""
        self.love_button.grid()  # Make button visible for all tracks
        if track.loved:
            self.love_button.config(text="♥", foreground="red", font=('Arial', 14, 'bold'))
        else:
            self.love_button.config(text="♡", foreground="gray", font=('Arial', 14))
    
    def toggle_play_pause(self):
        """Toggle between play and pause"""
        if not self.audio_available:
            return
        
        if not self.current_playlist:
            return
        
        if self.is_playing:
            # Capture current position before pausing
            if self._segment_start_mono is not None:
                elapsed = time.monotonic() - self._segment_start_mono
                self._seek_base_sec = self._seek_base_sec + elapsed
            
            pygame.mixer.music.pause()
            self.is_playing = False
            print("Paused")
        else:
            # Reset segment start time when unpausing
            self._segment_start_mono = time.monotonic()
            
            pygame.mixer.music.unpause()
            if pygame.mixer.music.get_busy():
                self.is_playing = True
                print("Unpaused")
            else:
                self.play_track_at_index(self.current_index)
                print("Started playing")
    
    def on_progress_click(self, event):
        """Handle click on progress bar to seek"""
        if not self.audio_available or not self.current_playlist or self.current_index < 0:
            return
        
        try:
            bar_width = self.progress_bar.winfo_width()
            click_x = event.x
            
            if click_x < 0:
                click_x = 0
            elif click_x > bar_width:
                click_x = bar_width
            
            percentage = (click_x / bar_width) * 100
            
            track = self.current_playlist[self.current_index]
            if track.duration <= 0:
                return
            
            target_pos = (percentage / 100.0) * track.duration
            
            try:
                # pygame.mixer.music.set_pos() is unreliable with MP3/FLAC/M4A
                # Better approach: reload and play from position
                was_playing = self.is_playing
                
                # Determine playback path (converted M4A or original)
                playback_path = track.filepath
                if track.filepath.lower().endswith('.m4a'):
                    # Use converted cache path
                    cache_path = self._get_cache_path(track.filepath)
                    if cache_path.exists():
                        playback_path = str(cache_path)
                    else:
                        # Can't seek to uncached M4A - would block UI during conversion
                        self.status_label.config(text="⚠ Cannot seek during M4A conversion")
                        return
                
                # Reload the track
                pygame.mixer.music.load(str(playback_path))
                
                # Play from target position (start parameter is in seconds)
                pygame.mixer.music.play(start=target_pos)
                if not was_playing:
                    pygame.mixer.music.pause()
                
                # Update position tracking
                self._seek_base_sec = float(target_pos)
                self._segment_start_mono = time.monotonic()
                
                # Update UI
                self.progress_var.set(percentage)
                
                current_min = int(target_pos // 60)
                current_sec = int(target_pos % 60)
                total_min = int(track.duration // 60)
                total_sec = int(track.duration % 60)
                self.time_label.config(text=f"{current_min}:{current_sec:02d} / {total_min}:{total_sec:02d}")
                
                self.root.update_idletasks()
                
                print(f"Seeked to {target_pos:.1f}s ({percentage:.1f}%)")
            except (pygame.error, OSError, IOError) as e:
                print(f"⚠ Seeking failed: {e}")
            except Exception as e:
                print(f"⚠ Unexpected seek error: {e}")
                import traceback
                traceback.print_exc()
        
        except Exception as e:
            print(f"⚠ Error in seek handler: {e}")
    
    def on_volume_change(self, value):
        """Handle volume slider change - controls system volume and pygame"""
        # Ignore if we're programmatically updating the slider
        if self._updating_volume:
            return
        
        try:
            volume_percent = float(value)
            
            # Set system volume if available
            system_set = False
            if self.volume_interface:
                system_set = self.set_system_volume(volume_percent)
            
            # Always set pygame volume as backup/fallback
            # This ensures volume control works even if system control fails mid-session
            if self.audio_available:
                pygame.mixer.music.set_volume(volume_percent / 100.0)
            
            # Save preference
            try:
                with open(self.volume_pref_file, 'w') as f:
                    f.write(str(volume_percent))
            except (OSError, IOError):
                pass  # Preferences not critical
        except (ValueError, TypeError) as e:
            print(f"⚠ Invalid volume value: {e}")
        except Exception as e:
            print(f"⚠ Error setting volume: {e}")
    
    # ============================================================
    # V2: Unified Player Metadata System
    # ============================================================
    
    def _load_player_metadata(self):
        """Load unified player metadata from XML file with backup recovery"""
        if not self.player_metadata_file.exists():
            print("Player metadata file not found - will create on first save")
            return
        
        # Try to load, with backup recovery
        backup_file = self.player_metadata_file.with_suffix('.xml.backup')
        
        for attempt, filepath in enumerate([self.player_metadata_file, backup_file], 1):
            if not filepath.exists():
                continue
            
            try:
                print(f"Loading player metadata (attempt {attempt})...")
                with open(filepath, 'rb') as f:
                    data = plistlib.load(f)
                
                tracks = data.get('tracks', {})

                # Thread-safe write to metadata
                with self._metadata_lock:
                    for filepath_key, track_data in tracks.items():
                        # Normalize key for storage
                        key = self._norm_path(filepath_key)
                        self.player_metadata[key] = {
                            'play_count': int(track_data.get('play_count', 0)),
                            'skip_count': int(track_data.get('skip_count', 0)),
                            'rating': int(track_data.get('rating', 0)),
                            'last_played': int(track_data.get('last_played', 0)),
                            'loved': bool(track_data.get('loved', False)),
                            'date_added': int(track_data.get('date_added', 0)),
                        }

                print(f"✓ Loaded metadata for {len(tracks)} tracks")
                
                # If we loaded from backup, restore it as main
                if filepath == backup_file:
                    print("  Restored from backup file")
                    try:
                        import shutil
                        shutil.copy2(backup_file, self.player_metadata_file)
                    except (OSError, IOError) as e:
                        print(f"  Could not restore backup: {e}")
                
                return  # Success
                
            except Exception as e:
                print(f"⚠ Player metadata file corrupted (attempt {attempt}): {e}")
                if filepath == self.player_metadata_file:
                    print("  Trying backup file...")
                continue
        
        # Both files failed
        print("⚠ Could not load metadata from primary or backup, starting fresh")
        try:
            self.player_metadata_file.unlink()
        except (OSError, IOError):
            pass
        try:
            backup_file.unlink()
        except (OSError, IOError):
            pass
    
    def _save_player_metadata(self):
        """Save unified player metadata to XML file (atomic write with backup)"""
        try:
            # Copy metadata under lock to avoid holding lock during I/O
            with self._metadata_lock:
                metadata_copy = dict(self.player_metadata)

            tracks_out = {}
            for filepath_norm, track_data in metadata_copy.items():
                tracks_out[filepath_norm] = {
                    'play_count': int(track_data.get('play_count') or 0),
                    'skip_count': int(track_data.get('skip_count') or 0),
                    'rating': int(track_data.get('rating') or 0),
                    'last_played': int(track_data.get('last_played') or 0),
                    'loved': bool(track_data.get('loved', False)),  # Ensure boolean
                    'date_added': int(track_data.get('date_added') or 0),
                }
            
            # Create backup of existing file before overwriting
            if self.player_metadata_file.exists():
                backup_file = self.player_metadata_file.with_suffix('.xml.backup')
                try:
                    import shutil
                    shutil.copy2(self.player_metadata_file, backup_file)
                except (OSError, IOError):
                    pass  # Backup failed, but continue with save
            
            # Atomic write: write to temp file, then replace
            tmp_path = self.player_metadata_file.with_suffix(self.player_metadata_file.suffix + '.tmp')
            with open(tmp_path, 'wb') as f:
                plistlib.dump({'tracks': tracks_out}, f)
            
            # Verify the temp file before replacing
            if tmp_path.stat().st_size < 100:
                raise IOError("Metadata file suspiciously small, not overwriting")
            
            tmp_path.replace(self.player_metadata_file)
            
        except Exception as e:
            print(f"⚠ Error saving player metadata: {e}")
            # Try to clean up temp file
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except (OSError, IOError, NameError):
                pass

    def _update_track_metadata(self, track):
        """Update metadata for any track (unified system)"""
        filepath_to_check = getattr(track, 'original_filepath', track.filepath)

        # Normalize key for storage
        fp = self._norm_path(filepath_to_check)

        # Store metadata in memory (thread-safe)
        with self._metadata_lock:
            self.player_metadata[fp] = {
                'play_count': int(getattr(track, 'play_count', 0) or 0),
                'skip_count': int(getattr(track, 'skips', 0) or 0),
                'rating': int(getattr(track, 'rating', 0) or 0),
                'last_played': int(getattr(track, 'last_played', 0) or 0),
                'loved': bool(getattr(track, 'loved', None)),  # True/False/None -> True/False/False
                'date_added': int(getattr(track, 'date_added', 0) or 0),
            }

        # Queue async save (debounced to avoid blocking UI)
        self._queue_metadata_save()
    
    def _queue_metadata_save(self):
        """Queue metadata save in background (debounced)"""
        # Start worker thread if not running
        if self._metadata_save_thread is None or not self._metadata_save_thread.is_alive():
            self._metadata_save_stop.clear()
            self._metadata_save_thread = threading.Thread(
                target=self._metadata_save_worker, 
                daemon=True,
                name="MetadataSaveWorker"
            )
            self._metadata_save_thread.start()
        
        # Queue a save request (will be debounced by worker)
        try:
            self._metadata_save_queue.put_nowait(True)
        except queue.Full:
            pass  # Queue full = save already pending
    
    def _metadata_save_worker(self):
        """Background thread that processes metadata saves"""
        while not self._metadata_save_stop.is_set():
            try:
                # Wait for save request with timeout
                self._metadata_save_queue.get(timeout=0.5)
                
                # Debounce: wait a bit in case more saves are coming
                time.sleep(METADATA_SAVE_DEBOUNCE_SEC)
                
                # Drain queue (multiple rapid saves = one actual save)
                while not self._metadata_save_queue.empty():
                    try:
                        self._metadata_save_queue.get_nowait()
                    except queue.Empty:
                        break
                
                # Do the actual save
                self._save_player_metadata()
                
            except queue.Empty:
                continue
            except Exception as e:
                print(f"⚠ Metadata save error: {e}")
    
    def _cleanup_orphaned_metadata(self):
        """Remove metadata for files no longer in library"""
        if not self.all_tracks:
            return

        # Build set of valid normalized paths
        valid_paths = {self._norm_path(getattr(t, 'original_filepath', t.filepath))
                       for t in self.all_tracks}

        # Find and remove orphaned entries (thread-safe)
        with self._metadata_lock:
            orphaned = [fp for fp in self.player_metadata.keys() if fp not in valid_paths]
            for fp in orphaned:
                del self.player_metadata[fp]

        if orphaned:
            print(f"🗑 Cleaned up {len(orphaned)} orphaned metadata entries")
            # Save immediately after cleanup
            self._save_player_metadata()
    
    def _load_album_artwork(self, track):
        """Load and display album artwork for a track"""
        try:
            audio = MutagenFile(track.meta_path)
            artwork_data = None
            
            # Try embedded artwork first
            if isinstance(audio, MP3):
                if audio.tags:
                    for key in audio.tags.keys():
                        if key.startswith('APIC'):
                            artwork_data = audio.tags[key].data
                            break
            elif isinstance(audio, MP4):
                if audio.tags and 'covr' in audio.tags:
                    artwork_data = bytes(audio.tags['covr'][0])
            elif isinstance(audio, FLAC):
                # FLAC files store pictures in the 'pictures' list
                if audio.pictures:
                    artwork_data = audio.pictures[0].data
            
            # If no embedded artwork, look for external image files
            if not artwork_data:
                track_dir = os.path.dirname(track.filepath)
                
                # Priority list of common artwork filenames
                priority_filenames = [
                    'cover', 'folder', 'albumart', 'front', 'album', 'artwork'
                ]
                
                # Try priority filenames first with all image extensions
                for base_name in priority_filenames:
                    for ext in ['.jpg', '.jpeg', '.png', '.JPG', '.JPEG', '.PNG']:
                        artwork_path = os.path.join(track_dir, base_name + ext)
                        if os.path.exists(artwork_path):
                            try:
                                with open(artwork_path, 'rb') as f:
                                    artwork_data = f.read()
                                print(f"✓ Found external artwork: {base_name}{ext}")
                                break
                            except Exception as e:
                                continue
                    if artwork_data:
                        break
                
                # If still no artwork, scan directory for ANY image file
                if not artwork_data:
                    try:
                        for filename in os.listdir(track_dir):
                            lower_name = filename.lower()
                            if lower_name.endswith(('.jpg', '.jpeg', '.png')):
                                artwork_path = os.path.join(track_dir, filename)
                                try:
                                    with open(artwork_path, 'rb') as f:
                                        artwork_data = f.read()
                                    print(f"✓ Found external artwork: {filename}")
                                    break
                                except:
                                    continue
                    except:
                        pass
            
            if artwork_data:
                image = Image.open(io.BytesIO(artwork_data))
                image.thumbnail((ARTWORK_SIZE, ARTWORK_SIZE), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image)
                self.current_artwork = photo
                self.artwork_label.config(image=photo)
                return True
            else:
                self._show_placeholder_artwork()
                return False
                
        except (OSError, IOError):
            # File read errors are expected (missing files, permissions)
            self._show_placeholder_artwork()
            return False
        except Exception as e:
            print(f"⚠ Unexpected error loading artwork: {e}")
            self._show_placeholder_artwork()
            return False
    
    def _show_placeholder_artwork(self):
        """Show placeholder when no artwork available"""
        try:
            theme = self.themes['dark'] if self.dark_mode else self.themes['light']
            color = theme['placeholder']
            rgb = tuple(int(color[i:i+2], 16) for i in (1, 3, 5))
            
            placeholder = Image.new('RGB', (ARTWORK_SIZE, ARTWORK_SIZE), color=rgb)
            photo = ImageTk.PhotoImage(placeholder)
            self.current_artwork = photo
            self.artwork_label.config(image=photo)
        except (ValueError, KeyError, AttributeError) as e:
            print(f"⚠ Could not create placeholder: {e}")
            self.artwork_label.config(image='')
    
    def locate_ffmpeg(self):
        """Try to locate FFmpeg executable"""
        try:
            result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, 
                                   encoding='utf-8', errors='ignore', timeout=2)
            if result.returncode == 0:
                print("✓ FFmpeg found in PATH")
                return "ffmpeg"
        except:
            pass
        
        winget_link = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
        if winget_link.exists():
            try:
                result = subprocess.run([str(winget_link), "-version"], capture_output=True, text=True,
                                       encoding='utf-8', errors='ignore', timeout=2)
                if result.returncode == 0:
                    print(f"✓ FFmpeg found at WinGet link: {winget_link}")
                    return str(winget_link)
            except:
                pass
        
        common_paths = [
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        ]
        
        try:
            winget_path = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
            if winget_path.exists():
                for item in winget_path.iterdir():
                    if "ffmpeg" in item.name.lower():
                        for root, dirs, files in os.walk(item):
                            if "ffmpeg.exe" in files:
                                ffmpeg_exe = os.path.join(root, "ffmpeg.exe")
                                common_paths.insert(0, ffmpeg_exe)
                                break
        except:
            pass
        
        for path in common_paths:
            if os.path.exists(path):
                try:
                    result = subprocess.run([path, "-version"], capture_output=True, text=True,
                                           encoding='utf-8', errors='ignore', timeout=2)
                    if result.returncode == 0:
                        print(f"✓ FFmpeg found at: {path}")
                        return path
                except:
                    pass
        
        print("✗ FFmpeg not found in PATH or common locations")
        print("  Try: Settings > Set FFmpeg Path")
        return None
    
    def check_ffmpeg(self):
        """Check if FFmpeg is available"""
        if self.ffmpeg_path:
            print(f"✓ Using FFmpeg: {self.ffmpeg_path}")
            return True
        
        try:
            result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                                   encoding='utf-8', errors='ignore', timeout=2)
            if result.returncode == 0:
                print("⚠ FFmpeg command works but wasn't auto-detected")
                response = messagebox.askyesno(
                    "FFmpeg Detection Issue",
                    "FFmpeg appears to be installed but wasn't auto-detected.\n\n"
                    "Would you like to use the 'ffmpeg' command?\n\n"
                    "Click Yes to use it, or No to manually set the path."
                )
                if response:
                    self.ffmpeg_path = "ffmpeg"
                    print("✓ Using FFmpeg via command")
                    return True
                else:
                    self.set_ffmpeg_path()
                    return bool(self.ffmpeg_path)
        except:
            pass
        
        messagebox.showwarning(
            "FFmpeg Not Found",
            "FFmpeg is required to play .m4a files.\n\n"
            "To fix:\n"
            "• Use Settings > Set FFmpeg Path to locate it manually\n"
            "• Or install: winget install FFmpeg\n"
            "• Then restart this app\n\n"
            ".m4a files will be skipped until FFmpeg is configured."
        )
        return False
    
    def _convert_m4a(self, original_path):
        """Convert .m4a to .mp3 using FFmpeg, cache the result (atomic write)"""
        if not self.ffmpeg_path:
            return None
        
        output_path = self._get_cache_path(original_path)
        
        if output_path.exists():
            try:
                original_mtime = os.path.getmtime(original_path)
                cache_mtime = os.path.getmtime(output_path)
                cache_size = output_path.stat().st_size
                if original_mtime <= cache_mtime and cache_size > MIN_VALID_CACHE_SIZE:
                    return str(output_path)
                else:
                    output_path.unlink(missing_ok=True)
            except (OSError, IOError, ValueError):
                # If anything goes weird, fall through to reconvert
                try:
                    output_path.unlink(missing_ok=True)
                except (OSError, IOError):
                    pass
        
        # Compute hash for temp filename
        import hashlib
        hash_key = hashlib.md5(str(original_path).encode()).hexdigest()
        
        # Write to unique temp file, then atomically rename
        # Include PID and thread ID to prevent collisions between background/foreground conversion
        # IMPORTANT: Use .mp3 extension so FFmpeg correctly infers output format
        tmp_name = f"{hash_key}.{os.getpid()}.{threading.get_ident()}.mp3"
        tmp_path = self.convert_cache_dir / tmp_name
        
        try:
            subprocess.run(
                [self.ffmpeg_path, "-y", "-i", original_path, "-c:a", "libmp3lame", "-q:a", "2", str(tmp_path)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                check=True,
                timeout=30
            )
            
            # Verify output before renaming
            if tmp_path.exists() and tmp_path.stat().st_size > MIN_VALID_CACHE_SIZE:
                tmp_path.replace(output_path)  # Atomic on Windows when same volume
                return str(output_path)
            
            # FFmpeg "succeeded" but no valid output
            try:
                tmp_path.unlink(missing_ok=True)
            except:
                pass
            return None
            
        except subprocess.CalledProcessError as e:
            try:
                tmp_path.unlink(missing_ok=True)
            except (OSError, IOError):
                pass  # File already deleted or inaccessible
            
            # Concise error for corrupted/DRM files
            error_text = (e.stderr or "").lower()
            if any(x in error_text for x in ['invalid band type', 'reserved bit', 'drm', 'prediction']):
                print(f"⚠ Skipping corrupted/DRM file: {Path(original_path).name}")
            else:
                print(f"⚠ FFmpeg failed: {Path(original_path).name}")
            return None
        except subprocess.TimeoutExpired:
            try:
                tmp_path.unlink(missing_ok=True)
            except (OSError, IOError):
                pass  # File already deleted or inaccessible
            print(f"⚠ FFmpeg conversion timeout for {original_path}")
            return None
        except Exception as e:
            try:
                tmp_path.unlink(missing_ok=True)
            except (OSError, IOError):
                pass  # File already deleted or inaccessible
            print(f"⚠ Unexpected error converting {original_path}: {e}")
            return None
    
    # Background Pre-Conversion Methods
    
    def _norm_path(self, p):
        """Normalize path for cross-platform comparison"""
        return os.path.normcase(os.path.normpath(p or ""))
    
    def _get_cache_path(self, original_path):
        """Get the cache path for a converted M4A file"""
        import hashlib
        hash_key = hashlib.md5(str(original_path).encode()).hexdigest()
        return self.convert_cache_dir / f"{hash_key}.mp3"
    
    def start_background_preconversion(self):
        """
        V2: Start background pre-conversion of iTunes library .m4a files.
        Converts .m4a files from iTunes Media folder (not M4P Downloads).
        """
        if not self.ffmpeg_path:
            return
        
        if not getattr(self, 'all_tracks', None):
            return
        
        # Check disk space before starting conversion
        if not self._check_disk_space(MIN_DISK_SPACE_PRECONV_MB):
            print("⚠ Skipping pre-conversion due to low disk space")
            return
        
        # Stop any existing worker
        self.stop_background_preconversion()
        
        # V2: Filter by path - iTunes Media folder .m4a files only
        m4a_tracks = [
            t for t in self.all_tracks
            if (getattr(t, 'filepath', '') or '').lower().endswith('.m4a')
            and "itunes/itunes media" in getattr(t, 'filepath', '').lower().replace('\\', '/')
            and "m4p downloads" not in getattr(t, 'filepath', '').lower()
        ]
        
        if not m4a_tracks:
            return
        
        # Filter out already-converted files to avoid redundant work
        files_needing_conversion = []
        for track in m4a_tracks:
            output_path = self._get_cache_path(track.filepath)
            
            # Only queue if not cached or cache is stale
            if not output_path.exists():
                files_needing_conversion.append(track.filepath)
            else:
                try:
                    original_mtime = os.path.getmtime(track.filepath)
                    cache_mtime = os.path.getmtime(output_path)
                    cache_size = output_path.stat().st_size
                    if original_mtime > cache_mtime or cache_size < MIN_VALID_CACHE_SIZE:
                        files_needing_conversion.append(track.filepath)
                except (OSError, IOError, AttributeError):
                    files_needing_conversion.append(track.filepath)
        
        if not files_needing_conversion:
            print(f"✓ All {len(m4a_tracks)} iTunes .m4a files already converted")
            return
        
        # Increment token to cancel any previous runs
        self._preconv_token = getattr(self, '_preconv_token', 0) + 1
        
        self._preconv_total = len(files_needing_conversion)
        self._preconv_done = 0
        self._preconv_failed = 0
        self._preconv_stop.clear()
        
        # Enqueue only files needing conversion
        for filepath in files_needing_conversion:
            self._preconv_q.put(filepath)
        
        # Start worker thread
        self._preconv_thread = threading.Thread(target=self._preconvert_worker, daemon=True)
        self._preconv_thread.start()
        
        already_cached = len(m4a_tracks) - len(files_needing_conversion)
        print(f"🔄 Pre-converting {self._preconv_total} new .m4a files ({already_cached} already cached)...")
    
    def stop_background_preconversion(self):
        """Stop background conversion and clear queue"""
        self._preconv_stop.set()
        
        # Increment token to signal cancellation
        self._preconv_token = getattr(self, '_preconv_token', 0) + 1
        
        # Drain queue
        try:
            while True:
                self._preconv_q.get_nowait()
                self._preconv_q.task_done()
        except queue.Empty:
            pass
        
        # Wait for thread to finish (with timeout)
        if self._preconv_thread and self._preconv_thread.is_alive():
            self._preconv_thread.join(timeout=2.0)
    
    def _preconvert_worker(self):
        """Background thread that converts queued .m4a files to mp3 cache"""
        my_token = getattr(self, '_preconv_token', 0)
        
        while not self._preconv_stop.is_set():
            # Check if we've been cancelled via token
            if getattr(self, '_preconv_token', 0) != my_token:
                print("⏹ Pre-conversion cancelled (new scan started)")
                return
            
            try:
                original_path = self._preconv_q.get(timeout=0.25)
            except queue.Empty:
                break
            
            try:
                # Call _convert_m4a - it handles cache checking and atomic writes
                result = self._convert_m4a(original_path)
                if result is None:
                    self._preconv_failed += 1
            except Exception as e:
                print(f"⚠ Pre-conversion error for {os.path.basename(original_path)}: {e}")
                self._preconv_failed += 1
            finally:
                self._preconv_done += 1
                self._preconv_q.task_done()
                
                # Small yield to keep UI responsive
                time.sleep(0.05)
        
        # Final status update (wrapped to prevent crash if window closes)
        try:
            self.root.after(0, self._finish_preconv_status)
        except (RuntimeError, tk.TclError):
            pass
    
    def _finish_preconv_status(self):
        """Show completion status for pre-conversion (console only)"""
        if self._preconv_total > 0:
            success = self._preconv_done - self._preconv_failed
            if self._preconv_failed > 0:
                print(f"✓ iTunes library pre-conversion complete: {success}/{self._preconv_total} cached, {self._preconv_failed} failed")
            else:
                print(f"✓ iTunes library pre-conversion complete: {self._preconv_done}/{self._preconv_total} files cached")
    
    def _on_close(self):
        """Graceful shutdown: stop background processes and close"""
        # Stop metadata save worker
        self._metadata_save_stop.set()
        if self._metadata_save_thread and self._metadata_save_thread.is_alive():
            # Queue one final save
            try:
                self._metadata_save_queue.put(True, timeout=0.1)
            except queue.Full:
                pass
            self._metadata_save_thread.join(timeout=1.0)

        # Final metadata save (synchronous to ensure it completes)
        try:
            self._save_player_metadata()
        except (OSError, IOError, Exception) as e:
            print(f"⚠ Error saving metadata on close: {e}")

        try:
            self.stop_background_preconversion()
        except (RuntimeError, Exception) as e:
            print(f"⚠ Error stopping preconversion: {e}")

        # Release COM volume interface
        try:
            if self.volume_interface:
                self.volume_interface = None
        except (AttributeError, Exception):
            pass

        try:
            import keyboard
            keyboard.unhook_all_hotkeys()
        except (ImportError, Exception):
            pass

        try:
            self.root.destroy()
        except (tk.TclError, RuntimeError):
            pass
    
    def scan_library(self):
        """Scan music directories for tracks with caching"""
        if hasattr(self, '_scanning') and self._scanning:
            messagebox.showinfo("Scanning", "A scan is already in progress")
            return
        
        # Stop any ongoing background conversion
        self.stop_background_preconversion()
        
        self._scanning = True
        self.status_label.config(text="Starting library scan...")
        
        thread = threading.Thread(target=self._scan_library_thread, daemon=True)
        thread.start()
    
    def _scan_library_thread(self):
        """Background thread for library scanning"""
        try:
            # Helper to prevent crashes if window closes during scan
            def safe_after(delay, cb):
                try:
                    self.root.after(delay, cb)
                except:
                    pass
            
            self.root.after(0, lambda: self.status_label.config(text="Loading library..."))
            
            cached_data = self.load_cache()
            
            temp_all_tracks = []
            supported_extensions = {'.mp3', '.m4a', '.flac', '.ogg', '.wav', '.aac'}
            
            current_files = {}
            skipped_files = []  # Track files that were skipped
            
            for music_dir in self.music_dirs:
                if not os.path.exists(music_dir):
                    continue
                
                for root, dirs, files in os.walk(music_dir):
                    for file in files:
                        ext = os.path.splitext(file)[1].lower()
                        if ext in supported_extensions:
                            filepath = os.path.join(root, file)
                            try:
                                mtime = os.path.getmtime(filepath)
                                current_files[filepath] = mtime
                            except Exception as e:
                                skipped_files.append((filepath, str(e)))
                        elif ext in {'.jpg', '.png', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff',  # Images
                                     '.xml', '.json', '.txt', '.nfo', '.ini', '.log',  # Metadata/Config
                                     '.frag', '.initfrag', '.m3u8', '.descriptor',  # Streaming/HLS
                                     '.cue', '.m3u', '.pls', '.wpl',  # Playlists
                                     '.db', '.dat', '.cache', '.tmp'}:  # System files
                            # Skip known non-audio files silently
                            pass
                        elif ext:  # Has extension but not supported
                            skipped_files.append((os.path.join(root, file), f"Unsupported extension: {ext}"))
            
            self._last_scan_total_files = len(current_files)
            
            file_types = {}
            for filepath in current_files.keys():
                ext = os.path.splitext(filepath)[1].lower()
                file_types[ext] = file_types.get(ext, 0) + 1
            
            if self.debug_scan_logging:
                print("\n" + "="*60)
                print("SCAN DEBUG - File types found:")
                for ext, count in sorted(file_types.items(), key=lambda x: x[1], reverse=True):
                    print(f"  {ext}: {count} files")
                print(f"Total files found: {len(current_files)}")
                print("="*60 + "\n")
            
            files_to_scan = []
            
            if cached_data:
                cached_mtimes = cached_data.get('mtimes', {})
                cached_tracks = cached_data.get('tracks', {})
                
                if self.debug_scan_logging:
                    print(f"\n[CACHE] Found {len(cached_tracks)} tracks in cache")

                cached_loaded = 0
                cached_skipped = 0
                for filepath, mtime in current_files.items():
                    cached_mtime = cached_mtimes.get(filepath)
                    if cached_mtime is None or cached_mtime != mtime:
                        files_to_scan.append(filepath)
                    else:
                        if filepath in cached_tracks:
                            track = cached_tracks[filepath]

                            if not os.path.exists(track.filepath):
                                files_to_scan.append(filepath)
                                if self.debug_scan_logging:
                                    print(f"⚠ Cache broken for {os.path.basename(filepath)} - will re-process")
                            else:
                                # V2: Apply unified player metadata
                                norm_fp = self._norm_path(filepath)
                                player_meta = self.player_metadata.get(norm_fp)
                                if player_meta:
                                    track._apply_player_metadata(player_meta)

                                temp_all_tracks.append(track)
                                cached_loaded += 1
                        else:
                            files_to_scan.append(filepath)
                            cached_skipped += 1

                if self.debug_scan_logging:
                    print(f"[CACHE] Loaded {cached_loaded} tracks from cache")
                    print(f"[CACHE] {cached_skipped} files in current_files not found in cache")
                    print(f"[CACHE] {len(files_to_scan)} files need scanning (new/modified/broken)")
            else:
                if self.debug_scan_logging:
                    print("[CACHE] No cache found - will scan all files")
                files_to_scan = list(current_files.keys())
            
            if files_to_scan:
                self.root.after(0, lambda: self.status_label.config(
                    text=f"Scanning {len(files_to_scan)} new/modified files..."))
                
                for i, filepath in enumerate(files_to_scan):
                    try:
                        if i % 10 == 0:
                            progress = f"Scanning {i+1}/{len(files_to_scan)} files..."
                            self.root.after(0, lambda p=progress: self.status_label.config(text=p))
                        
                        # V2: Get unified player metadata for this file
                        norm_fp = self._norm_path(filepath)
                        metadata = self.player_metadata.get(norm_fp)
                        
                        track = MusicTrack(filepath, meta_path=filepath, player_metadata=metadata)
                        temp_all_tracks.append(track)
                    
                    except (OSError, IOError) as e:
                        print(f"⚠ Cannot read file {os.path.basename(filepath)}: {e}")
                    except Exception as e:
                        print(f"⚠ Error scanning {os.path.basename(filepath)}: {e}")
                        # Don't crash the whole scan for one bad file
                
                final_status = f"✓ Scanned {len(files_to_scan)} files - Total: {len(temp_all_tracks)} tracks"
                self.root.after(0, lambda: self.status_label.config(text=final_status))
            else:
                final_status = f"✓ Loaded {len(temp_all_tracks)} tracks from cache"
                self.root.after(0, lambda: self.status_label.config(text=final_status))
            
            if self.debug_scan_logging:
                print("\n" + "="*60)
                print("SCAN COMPLETE - Final track counts:")
                final_types = {}
                for track in temp_all_tracks:
                    ext = os.path.splitext(track.original_filepath if hasattr(track, 'original_filepath') else track.filepath)[1].lower()
                    final_types[ext] = final_types.get(ext, 0) + 1
                for ext, count in sorted(final_types.items(), key=lambda x: x[1], reverse=True):
                    print(f"  {ext}: {count} tracks")
                print(f"\nTotal tracks in library: {len(temp_all_tracks)}")
                print(f"Total files found on disk: {self._last_scan_total_files}")
                print(f"Difference: {self._last_scan_total_files - len(temp_all_tracks)} files NOT loaded")
                try:
                    with_loved      = sum(1 for t in temp_all_tracks if getattr(t, 'loved', None) is not None)
                    with_last_played = sum(1 for t in temp_all_tracks if getattr(t, 'last_played', None) is not None)
                    with_skips      = sum(1 for t in temp_all_tracks if getattr(t, 'skips', None) is not None)
                    with_date_added = sum(1 for t in temp_all_tracks if getattr(t, 'date_added', None) is not None)
                    if any([with_loved, with_last_played, with_skips, with_date_added]):
                        print("\nPlayer metadata found:")
                        if with_loved       > 0: print(f"  Loved: {with_loved} tracks")
                        if with_last_played > 0: print(f"  Last Played: {with_last_played} tracks")
                        if with_skips       > 0: print(f"  Skips: {with_skips} tracks")
                        if with_date_added  > 0: print(f"  Date Added: {with_date_added} tracks")
                except Exception:
                    pass
                print("="*60 + "\n")

            # Log any files that were skipped during discovery
            if skipped_files:
                print("⚠ Files skipped during scan:")
                for filepath, reason in skipped_files[:20]:  # Show first 20
                    print(f"  {os.path.basename(filepath)}: {reason}")
                if len(skipped_files) > 20:
                    print(f"  ... and {len(skipped_files) - 20} more")
                print()
            
            safe_after(0, lambda: self._finish_scan(temp_all_tracks, current_files))
            
        except Exception as e:
            print(f"Error during library scan: {e}")
            import traceback
            traceback.print_exc()
            safe_after(0, lambda: self.status_label.config(text=f"✗ Scan error: {e}"))
            safe_after(0, lambda: setattr(self, '_scanning', False))
    
    def _finish_scan(self, tracks, current_files):
        """Finish scan on main thread"""
        try:
            # Deduplicate tracks by normalized filepath
            seen = set()
            unique_tracks = []
            duplicates = 0
            
            for track in tracks:
                norm_path = self._norm_path(getattr(track, 'original_filepath', track.filepath))
                if norm_path not in seen:
                    seen.add(norm_path)
                    unique_tracks.append(track)
                else:
                    duplicates += 1
            
            if duplicates > 0:
                print(f"🗑 Removed {duplicates} duplicate entries")

            self.library_tracks = unique_tracks          # Task 1: full source-of-truth
            self.all_tracks = list(self.library_tracks)  # active working subset
            
            self.save_cache(current_files)
            
            # Remove metadata for deleted files
            self._cleanup_orphaned_metadata()
            
            self.update_library_stats()
            
            if self.all_tracks and self.audio_available:
                random_track = random.choice(self.all_tracks)
                self.current_playlist = [random_track]
                self.current_index = 0
                self.play_track_at_index(0)
            
            if self.all_tracks:
                self.apply_shuffle()
            
            # Start background pre-conversion after small delay (avoid status overlap)
            self.root.after(500, self.start_background_preconversion)
            
        finally:
            self._scanning = False
    
    def _normalize_cached_track(self, track):
        """Backwards-safe defaults for older pickles"""
        if not hasattr(track, "original_filepath"):
            track.original_filepath = track.filepath

        for name, default in (
            ("loved", None),
            ("last_played", None),
            ("skips", None),
            ("date_added", None),
        ):
            if not hasattr(track, name):
                setattr(track, name, default)

        for name, default in (
            ("rating", 0),
            ("play_count", 0),
            ("bpm", 0),
            ("genre", ""),
            ("year", ""),
            ("duration", 0),
        ):
            if not hasattr(track, name):
                setattr(track, name, default)

        return track
    
    def load_cache(self):
        """Load cached library data from JSON"""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # Convert track dicts back to MusicTrack objects
                tracks_data = (data or {}).get("tracks", {})
                tracks = {}
                for fp, track_dict in tracks_data.items():
                    try:
                        # Get player metadata for this track
                        norm_fp = self._norm_path(fp)
                        player_meta = self.player_metadata.get(norm_fp)
                        track = MusicTrack.from_dict(track_dict, player_metadata=player_meta)
                        track = self._normalize_cached_track(track)
                        tracks[fp] = track
                    except (KeyError, TypeError, ValueError) as e:
                        print(f"Error loading cached track {fp}: {e}")
                        continue

                data['tracks'] = tracks
                return data

        except json.JSONDecodeError as e:
            print(f"Error decoding cache JSON: {e}")
        except (OSError, IOError) as e:
            print(f"Error loading cache: {e}")
        except Exception as e:
            print(f"Unexpected error loading cache: {e}")
        return None

    def save_cache(self, file_mtimes):
        """Save library data to JSON cache"""
        # Check disk space before saving
        if not self._check_disk_space(MIN_DISK_SPACE_MB):
            print("⚠ Skipping cache save due to low disk space")
            return

        try:
            # Serialize tracks to dicts
            tracks_dict = {}
            for track in self.all_tracks:
                tracks_dict[track.filepath] = track.to_dict()

            cache_data = {
                'mtimes': file_mtimes,
                'tracks': tracks_dict
            }
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2)
        except (OSError, IOError, TypeError, ValueError) as e:
            print(f"Error saving cache: {e}")
    
    def force_rescan(self):
        """Force a full rescan of the library"""
        # Stop any ongoing background conversion
        self.stop_background_preconversion()
        
        try:
            if self.cache_file.exists():
                self.cache_file.unlink()
        except Exception as e:
            print(f"Error deleting cache: {e}")
        
        self.scan_library()
    
    def clear_cache(self):
        """Clear all cache files"""
        result = messagebox.askyesno(
            "Clear Cache",
            "This will delete:\n"
            "• Library cache\n"
            "• M4P Downloads metadata\n"
            "• All converted .mp3 files\n\n"
            "Songs will need to be converted again when played.\n"
            "M4P metadata will start fresh.\n\n"
            "Continue?"
        )
        
        if not result:
            return
        
        deleted_count = 0
        deleted_size = 0
        
        try:
            if self.cache_file.exists():
                deleted_size += self.cache_file.stat().st_size
                self.cache_file.unlink()
                deleted_count += 1
            
            # V2: Clear unified player metadata
            if self.player_metadata_file.exists():
                deleted_size += self.player_metadata_file.stat().st_size
                self.player_metadata_file.unlink()
                deleted_count += 1
            
            if self.convert_cache_dir.exists():
                for file in self.convert_cache_dir.iterdir():
                    if file.is_file():
                        deleted_size += file.stat().st_size
                        file.unlink()
                        deleted_count += 1
            
            deleted_mb = deleted_size / (1024 * 1024)
            messagebox.showinfo(
                "Cache Cleared",
                f"Deleted {deleted_count} files\n"
                f"Freed {deleted_mb:.1f} MB\n\n"
                "Library will rescan on next startup."
            )
            
        except Exception as e:
            messagebox.showerror("Error", f"Error clearing cache:\n{e}")
    
    def clear_metadata(self):
        """Clear all player metadata (play counts, loved status, etc.) with double confirmation"""
        # First confirmation
        result1 = messagebox.askyesno(
            "Clear All Metadata?",
            "⚠️ WARNING: This will permanently delete:\n\n"
            "• All play counts\n"
            "• All skip counts\n"
            "• All loved/favorited tracks\n"
            "• All last played dates\n"
            "• All ratings\n\n"
            f"This affects {len(self.player_metadata)} tracks.\n\n"
            "Are you sure you want to continue?",
            icon='warning'
        )
        
        if not result1:
            return
        
        # Second confirmation
        result2 = messagebox.askyesno(
            "Final Confirmation",
            "⚠️ FINAL WARNING ⚠️\n\n"
            "This cannot be undone!\n\n"
            "All metadata will be permanently deleted.\n"
            "You will start completely fresh.\n\n"
            "Are you absolutely sure?",
            icon='warning'
        )
        
        if not result2:
            return
        
        try:
            # Clear in-memory metadata
            track_count = len(self.player_metadata)
            self.player_metadata.clear()
            
            # Delete metadata file
            if self.player_metadata_file.exists():
                self.player_metadata_file.unlink()
            
            # Reset all loaded tracks
            for track in self.all_tracks:
                track.play_count = 0
                track.skips = None
                track.rating = 0
                track.last_played = None
                track.loved = None
                track.date_added = None
            
            # Update love button for current track if playing
            if self.is_playing and 0 <= self.current_index < len(self.current_playlist):
                current_track = self.current_playlist[self.current_index]
                self._update_love_button(current_track)
            
            messagebox.showinfo(
                "Metadata Cleared",
                f"✓ Successfully cleared metadata for {track_count} tracks\n\n"
                "All tracks reset to fresh state:\n"
                "• Play counts: 0\n"
                "• Loved status: none\n"
                "• Skip counts: 0\n"
                "• Ratings: 0\n\n"
                "Metadata will rebuild as you listen."
            )
            
        except Exception as e:
            messagebox.showerror("Error", f"Error clearing metadata:\n{e}")
    
    def show_shortcuts(self):
        """Show keyboard shortcuts dialog"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Keyboard Shortcuts")
        dialog.geometry("350x250")
        dialog.resizable(False, False)
        
        dialog.transient(self.root)
        dialog.grab_set()
        
        frame = ttk.Frame(dialog, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(frame, text="Keyboard Shortcuts", 
                 font=('Arial', 14, 'bold')).pack(pady=(0, 20))
        
        shortcuts = [
            ("Media keys", "Control playback globally"),
            ("", "(Play/Pause, Prev, Next, Stop)"),
            ("", ""),
            ("", "Works even when window not focused")
        ]
        
        for key, action in shortcuts:
            row = ttk.Frame(frame)
            row.pack(fill=tk.X, pady=5)
            
            ttk.Label(row, text=key, font=('Courier', 11, 'bold'), 
                     width=8).pack(side=tk.LEFT)
            ttk.Label(row, text=action, font=('Arial', 10)).pack(side=tk.LEFT)
        
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=20)
    
    def test_hotkeys(self):
        """Test if keyboard hotkeys are working"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Test Hotkeys")
        dialog.geometry("500x400")
        
        ttk.Label(dialog, text="Press F2-F8 to test hotkeys", 
                 font=('Arial', 12, 'bold')).pack(pady=10)
        
        text = tk.Text(dialog, height=15, width=55, wrap=tk.WORD)
        text.pack(pady=10, padx=10, fill=tk.BOTH, expand=True)
        
        text.insert(tk.END, "Expected hotkeys:\n")
        text.insert(tk.END, "  F2 = Volume Down\n")
        text.insert(tk.END, "  F3 = Volume Up\n")
        text.insert(tk.END, "  F4 = Mute Toggle\n")
        text.insert(tk.END, "  F5 = Stop\n")
        text.insert(tk.END, "  F6 = Previous\n")
        text.insert(tk.END, "  F7 = Play/Pause\n")
        text.insert(tk.END, "  F8 = Next\n\n")
        text.insert(tk.END, "Press any F2-F8 key and check the console for output.\n\n")
        
        try:
            import keyboard
            text.insert(tk.END, "✓ keyboard module is installed\n\n")
            
            # Check if running as admin
            try:
                import ctypes
                is_admin = ctypes.windll.shell32.IsUserAnAdmin()
                if is_admin:
                    text.insert(tk.END, "✓ Running as administrator\n")
                else:
                    text.insert(tk.END, "⚠ NOT running as administrator\n")
                    text.insert(tk.END, "  Some hotkeys may not work\n")
                    text.insert(tk.END, "  Right-click exe → Run as administrator\n")
            except:
                text.insert(tk.END, "? Could not check admin status\n")
            
            text.insert(tk.END, "\nIf hotkeys don't work:\n")
            text.insert(tk.END, "  1. Run as administrator\n")
            text.insert(tk.END, "  2. Close other apps using F keys\n")
            text.insert(tk.END, "  3. Check console for error messages\n")
            
        except ImportError:
            text.insert(tk.END, "✗ keyboard module NOT installed\n")
            text.insert(tk.END, "  Install: pip install keyboard\n")
            text.insert(tk.END, "  Then restart the app\n")
        
        text.config(state=tk.DISABLED)
        
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=10)
    
    def show_about(self):
        """Show about dialog"""
        messagebox.showinfo(
            "About Custom Shuffle Music Player",
            "Custom Shuffle Music Player\n\n"
            "A standalone music player with advanced shuffle algorithms\n\n"
            "Features:\n"
            "• Smart shuffle algorithms\n"
            "• .m4a support via FFmpeg\n"
            "• Metadata reading and display\n"
            "• On-demand file conversion\n"
            "• Keyboard shortcuts (media keys)\n\n"
            "Cache location:\n"
            f"{self.cache_file.parent}"
        )
    
    def show_diagnostics(self):
        """Show library diagnostics"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Library Diagnostics")
        dialog.geometry("700x600")
        
        text = tk.Text(dialog, height=30, width=80, wrap=tk.WORD)
        text.pack(pady=10, padx=10, fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(dialog, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        text.insert(tk.END, "=== LIBRARY DIAGNOSTICS ===\n\n")
        
        text.insert(tk.END, "AUDIO STATUS:\n")
        if self.audio_available:
            text.insert(tk.END, "  ✓ Audio device initialized\n")
        else:
            text.insert(tk.END, "  ✗ Audio device NOT available\n")
            text.insert(tk.END, "     Playback will not work\n")
        text.insert(tk.END, "\n")
        
        text.insert(tk.END, "FFMPEG STATUS:\n")
        if self.ffmpeg_path:
            text.insert(tk.END, f"  ✓ FFmpeg found: {self.ffmpeg_path}\n")
            try:
                result = subprocess.run([self.ffmpeg_path, "-version"], capture_output=True, text=True,
                                       encoding='utf-8', errors='ignore', timeout=2)
                if result.returncode == 0:
                    version_line = result.stdout.split('\n')[0]
                    text.insert(tk.END, f"  ✓ Version: {version_line}\n")
                    text.insert(tk.END, f"  ✓ .m4a files will be converted on-demand when played\n")
            except Exception as e:
                text.insert(tk.END, f"  ⚠ Error checking version: {e}\n")
        else:
            text.insert(tk.END, "  ✗ FFmpeg NOT FOUND\n")
            text.insert(tk.END, "     Install with: winget install FFmpeg\n")
            text.insert(tk.END, "     Then restart app\n")
            text.insert(tk.END, "     .m4a files will not be playable\n")
        text.insert(tk.END, "\n")
        
        text.insert(tk.END, "CACHE LOCATION:\n")
        text.insert(tk.END, f"  Directory: {self.cache_file.parent}\n")
        text.insert(tk.END, f"  Library cache: {self.cache_file.name}\n")
        text.insert(tk.END, f"  Player metadata: {self.player_metadata_file.name}\n")
        text.insert(tk.END, f"  Conversion cache: {self.convert_cache_dir.name}\n")
        
        cache_size = 0
        try:
            if self.cache_file.exists():
                cache_size += self.cache_file.stat().st_size
            if self.player_metadata_file.exists():
                cache_size += self.player_metadata_file.stat().st_size
            if self.convert_cache_dir.exists():
                for file in self.convert_cache_dir.iterdir():
                    if file.is_file():
                        cache_size += file.stat().st_size
            cache_mb = cache_size / (1024 * 1024)
            text.insert(tk.END, f"  Total cache size: {cache_mb:.1f} MB\n")
        except:
            pass
        text.insert(tk.END, "\n")
        
        text.insert(tk.END, "FOLDERS BEING SCANNED:\n")
        for folder in self.music_dirs:
            exists = "✓" if os.path.exists(folder) else "✗ (NOT FOUND)"
            text.insert(tk.END, f"  {exists} {folder}\n")
        text.insert(tk.END, "\n")
        
        text.insert(tk.END, f"CURRENT LIBRARY:\n")
        text.insert(tk.END, f"  Total tracks found: {len(self.all_tracks)}\n")
        artists = set(t.artist for t in self.all_tracks)
        albums = set(f"{t.artist}||{t.album}" for t in self.all_tracks)
        text.insert(tk.END, f"  Unique artists: {len(artists)}\n")
        text.insert(tk.END, f"  Unique albums: {len(albums)}\n\n")
        
        text.insert(tk.END, "FILE TYPES FOUND:\n")
        extensions = {}
        for track in self.all_tracks:
            ext = os.path.splitext(track.filepath)[1].lower()
            extensions[ext] = extensions.get(ext, 0) + 1
        
        for ext, count in sorted(extensions.items(), key=lambda x: x[1], reverse=True):
            text.insert(tk.END, f"  {ext}: {count} files\n")
        text.insert(tk.END, "\n")
        
        text.config(state=tk.DISABLED)
        
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=10)
    
    def show_metadata(self):
        """Show all player metadata in console"""
        print("\n" + "="*80)
        print("PLAYER METADATA DUMP")
        print("="*80)
        
        if not self.player_metadata:
            print("No metadata found - fresh state")
            print("="*80 + "\n")
            return
        
        print(f"Total tracks with metadata: {len(self.player_metadata)}\n")
        
        # Sort by filepath for easier reading
        sorted_metadata = sorted(self.player_metadata.items(), key=lambda x: x[0])
        
        for filepath, metadata in sorted_metadata:
            # Find track in library to get readable name
            track_name = os.path.basename(filepath)
            for track in self.all_tracks:
                if self._norm_path(track.filepath) == filepath:
                    track_name = f"{track.artist} - {track.title}"
                    break
            
            print(f"\nFile: {track_name}")
            print(f"  Path: {filepath}")
            print(f"  Play Count: {metadata.get('play_count', 0)}")
            print(f"  Skip Count: {metadata.get('skip_count', 0)}")
            print(f"  Loved: {metadata.get('loved', False)}")
            print(f"  Rating: {metadata.get('rating', 0)}")
            
            last_played = metadata.get('last_played', 0)
            if last_played:
                dt = datetime.fromtimestamp(last_played)
                print(f"  Last Played: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                print(f"  Last Played: Never")

            date_added = metadata.get('date_added', 0)
            if date_added:
                dt = datetime.fromtimestamp(date_added)
                print(f"  Date Added: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                print(f"  Date Added: Not set")
        
        print("\n" + "="*80)
        print(f"END OF METADATA ({len(self.player_metadata)} tracks)")
        print("="*80 + "\n")
        
        # Also show summary
        messagebox.showinfo(
            "Metadata Displayed",
            f"Metadata for {len(self.player_metadata)} tracks\n"
            "has been printed to the console.\n\n"
            "Check the console window for full details."
        )
    
    def check_file_path(self):
        """Check if a specific file path exists and is in the library"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Check File Path")
        dialog.geometry("700x400")
        
        ttk.Label(dialog, text="Enter file path to check:", 
                 font=('Arial', 10)).pack(pady=10)
        
        path_var = tk.StringVar()
        path_entry = ttk.Entry(dialog, textvariable=path_var, width=80)
        path_entry.pack(pady=5, padx=10)
        
        result_text = tk.Text(dialog, height=15, width=80, wrap=tk.WORD)
        result_text.pack(pady=10, padx=10, fill=tk.BOTH, expand=True)
        
        def check():
            result_text.delete(1.0, tk.END)
            filepath = path_var.get().strip()
            
            if not filepath:
                result_text.insert(tk.END, "Please enter a file path")
                return
            
            result_text.insert(tk.END, f"Checking: {filepath}\n\n")
            
            # Check if file exists
            if os.path.exists(filepath):
                result_text.insert(tk.END, "✓ File exists on disk\n")
                
                # Check file size
                size = os.path.getsize(filepath)
                result_text.insert(tk.END, f"  Size: {size:,} bytes ({size/1024/1024:.2f} MB)\n")
                
                # Check extension
                ext = os.path.splitext(filepath)[1].lower()
                supported = {'.mp3', '.m4a', '.flac', '.ogg', '.wav', '.aac'}
                if ext in supported:
                    result_text.insert(tk.END, f"✓ Extension {ext} is supported\n")
                else:
                    result_text.insert(tk.END, f"✗ Extension {ext} is NOT supported\n")
                    result_text.insert(tk.END, f"  Supported: {', '.join(sorted(supported))}\n")
                
                # Check if in music directories
                in_music_dir = False
                for music_dir in self.music_dirs:
                    if filepath.lower().startswith(music_dir.lower()):
                        in_music_dir = True
                        result_text.insert(tk.END, f"✓ File is in scanned directory: {music_dir}\n")
                        break
                
                if not in_music_dir:
                    result_text.insert(tk.END, f"✗ File is NOT in any scanned music directory\n")
                    result_text.insert(tk.END, f"  Scanned directories:\n")
                    for d in self.music_dirs:
                        result_text.insert(tk.END, f"    {d}\n")
                
                # Check if in library
                found_in_library = False
                for track in self.all_tracks:
                    if self._norm_path(track.filepath) == self._norm_path(filepath):
                        found_in_library = True
                        result_text.insert(tk.END, f"\n✓ FOUND IN LIBRARY\n")
                        result_text.insert(tk.END, f"  Title: {track.title}\n")
                        result_text.insert(tk.END, f"  Artist: {track.artist}\n")
                        result_text.insert(tk.END, f"  Album: {track.album}\n")
                        result_text.insert(tk.END, f"  Duration: {track.format_duration()}\n")
                        break
                
                if not found_in_library:
                    result_text.insert(tk.END, f"\n✗ NOT FOUND IN LIBRARY\n")
                    result_text.insert(tk.END, f"\nPossible reasons:\n")
                    if ext not in supported:
                        result_text.insert(tk.END, f"  • Unsupported file format\n")
                    if not in_music_dir:
                        result_text.insert(tk.END, f"  • Not in a scanned music directory\n")
                    result_text.insert(tk.END, f"  • File might be corrupted or unreadable\n")
                    result_text.insert(tk.END, f"  • Scan might need to be refreshed\n")
                    
            else:
                result_text.insert(tk.END, "✗ File does NOT exist at this path\n")
                result_text.insert(tk.END, "\nCheck for:\n")
                result_text.insert(tk.END, "  • Typos in the path\n")
                result_text.insert(tk.END, "  • File was moved or deleted\n")
                result_text.insert(tk.END, "  • Incorrect drive letter or directory\n")
        
        ttk.Button(dialog, text="Check", command=check).pack(pady=5)
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=5)
    
    def update_library_stats(self):
        """Update library statistics"""
        artists = set(t.artist for t in self.all_tracks)
        albums = set(f"{t.artist}||{t.album}" for t in self.all_tracks)
        
        self.total_tracks = len(self.all_tracks)
        self.total_artists = len(artists)
        self.total_albums = len(albums)
    
    def show_library_info(self):
        """Show library information dialog"""
        messagebox.showinfo(
            "Library Information",
            f"Total Tracks: {self.total_tracks}\n"
            f"Artists: {self.total_artists}\n"
            f"Albums: {self.total_albums}"
        )
    
    def change_folders(self):
        """Change music folder locations"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Music Folders")
        dialog.geometry("600x300")
        
        ttk.Label(dialog, text="Music folder paths (one per line):", 
                 font=('Arial', 10)).pack(pady=10)
        
        text = tk.Text(dialog, height=10, width=70)
        text.pack(pady=10, padx=10)
        
        for folder in self.music_dirs:
            text.insert(tk.END, folder + "\n")
        
        def save_folders():
            content = text.get("1.0", tk.END).strip()
            new_dirs = [line.strip() for line in content.split("\n") if line.strip()]
            self.music_dirs = new_dirs
            dialog.destroy()
            self.scan_library()
        
        ttk.Button(dialog, text="Save & Rescan", command=save_folders).pack(pady=10)
    
    def set_ffmpeg_path(self):
        """Manually set FFmpeg path"""
        current = self.ffmpeg_path if self.ffmpeg_path else "Not found"
        
        winget_link = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
        suggestion = ""
        if winget_link.exists() and not self.ffmpeg_path:
            suggestion = f"\n\nSuggested location found:\n{winget_link}"
        
        result = messagebox.askyesno(
            "Set FFmpeg Path",
            f"Current FFmpeg: {current}{suggestion}\n\n"
            "Do you want to browse for ffmpeg.exe?\n\n"
            "Or click No to use 'ffmpeg' command directly."
        )
        
        if result:
            filepath = filedialog.askopenfilename(
                title="Select ffmpeg.exe",
                initialdir=str(Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links"),
                filetypes=[("FFmpeg executable", "ffmpeg.exe"), ("All files", "*.*")]
            )
            
            if filepath:
                try:
                    result = subprocess.run([filepath, "-version"], capture_output=True, text=True,
                                           encoding='utf-8', errors='ignore', timeout=2)
                    if result.returncode == 0 and "ffmpeg" in result.stdout.lower():
                        self.ffmpeg_path = filepath
                        messagebox.showinfo("Success", f"FFmpeg path set to:\n{filepath}\n\n.m4a files will now convert on-demand when played.")
                    else:
                        messagebox.showerror("Error", "This doesn't appear to be a valid FFmpeg executable.")
                except Exception as e:
                    messagebox.showerror("Error", f"Failed to verify FFmpeg:\n{e}")
        else:
            try:
                result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                                       encoding='utf-8', errors='ignore', timeout=2)
                if result.returncode == 0:
                    self.ffmpeg_path = "ffmpeg"
                    messagebox.showinfo("Success", ".m4a files will now convert on-demand when played.")
                else:
                    messagebox.showerror("Error", "'ffmpeg' command doesn't work. Try browsing for ffmpeg.exe instead.")
            except:
                messagebox.showerror("Error", "'ffmpeg' command not found. Try browsing for ffmpeg.exe instead.")
    
    def filter_library(self):
        """Filter library by artist, album, genre, etc."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Filter Library")
        dialog.geometry("500x400")
        
        ttk.Label(dialog, text="Filter by:", font=('Arial', 12, 'bold')).pack(pady=10)
        
        filter_frame = ttk.Frame(dialog, padding="10")
        filter_frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(filter_frame, text="Artist contains:").grid(row=0, column=0, sticky=tk.W, pady=5)
        artist_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=artist_var, width=30).grid(row=0, column=1, pady=5)
        
        ttk.Label(filter_frame, text="Album contains:").grid(row=1, column=0, sticky=tk.W, pady=5)
        album_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=album_var, width=30).grid(row=1, column=1, pady=5)
        
        ttk.Label(filter_frame, text="Genre contains:").grid(row=2, column=0, sticky=tk.W, pady=5)
        genre_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=genre_var, width=30).grid(row=2, column=1, pady=5)
        
        ttk.Label(filter_frame, text="Min rating:").grid(row=3, column=0, sticky=tk.W, pady=5)
        rating_var = tk.IntVar(value=0)
        ttk.Spinbox(filter_frame, from_=0, to=100, textvariable=rating_var, width=10).grid(row=3, column=1, sticky=tk.W, pady=5)
        
        result_label = ttk.Label(dialog, text="", font=('Arial', 10))
        result_label.pack(pady=10)
        
        def apply_filter():
            artist_filter = artist_var.get().lower()
            album_filter  = album_var.get().lower()
            genre_filter  = genre_var.get().lower()
            min_rating    = rating_var.get()

            # Task 1: filter from full library_tracks (non-destructive)
            source = self.library_tracks if self.library_tracks else self.all_tracks
            filtered = [
                t for t in source
                if (not artist_filter or artist_filter in t.artist.lower())
                and (not album_filter  or album_filter  in t.album.lower())
                and (not genre_filter  or genre_filter  in t.genre.lower())
                and t.rating >= min_rating
            ]

            if filtered:
                self.all_tracks = filtered
                self.update_library_stats()
                self.apply_shuffle()
                dialog.destroy()
                messagebox.showinfo("Filter Applied", f"Filtered to {len(filtered)} tracks")
            else:
                result_label.config(text="No tracks match these filters!")

        def reset_filter():
            # Task 1: restore from library_tracks without triggering a full rescan
            if self.library_tracks:
                self.all_tracks = list(self.library_tracks)
                self.update_library_stats()
                self.apply_shuffle()
                dialog.destroy()
            else:
                self.scan_library()
                dialog.destroy()
        
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Apply Filter", command=apply_filter).grid(row=0, column=0, padx=5)
        ttk.Button(btn_frame, text="Reset to All", command=reset_filter).grid(row=0, column=1, padx=5)
    
    def apply_shuffle(self, algo=None):
        """Apply selected shuffle algorithm"""
        if not self.all_tracks:
            messagebox.showwarning("Warning", "No tracks loaded")
            return
        
        if algo is None:
            algo = "Smart Shuffle"
        
        # Always increment token to cancel any in-flight shuffle work
        self._shuffle_token = getattr(self, "_shuffle_token", 0) + 1
        token = self._shuffle_token
        
        self.status_label.config(text=f"Applying {algo}...")
        self.root.update_idletasks()
        
        if algo == "Smart Shuffle":
            self._set_navigation_enabled(False)
            
            tracks_snapshot = list(self.all_tracks)
            
            self.status_label.config(text="Shuffling...")
            
            def compute_shuffle():
                try:
                    history_snapshot = list(self._recent_play_history)
                    result = CustomShuffleAlgorithm.smart_shuffle(
                        tracks_snapshot, self.shuffle_config,
                        recent_history=history_snapshot,
                    )
                    
                    def apply_if_latest():
                        if getattr(self, "_shuffle_token", 0) != token:
                            # If this shuffle is stale, ensure nav isn't left disabled
                            self._set_navigation_enabled(True)
                            return
                        self._apply_shuffle_results(result, algo)
                    
                    try:
                        self.root.after(0, apply_if_latest)
                    except:
                        pass
                except Exception as e:
                    print(f"⚠ Smart Shuffle error: {e}")
                    try:
                        self.root.after(0, lambda: self.status_label.config(text="⚠ Shuffle failed"))
                    except:
                        pass
                    try:
                        self.root.after(0, lambda: self._set_navigation_enabled(True))
                    except:
                        pass
            
            threading.Thread(target=compute_shuffle, daemon=True).start()
            return
        
        # Non-smart shuffles should never leave nav disabled
        self._set_navigation_enabled(True)
        
        currently_playing_track = None
        if self.is_playing and self.current_playlist and 0 <= self.current_index < len(self.current_playlist):
            currently_playing_track = self.current_playlist[self.current_index]
        
        # Only Truly Random remains (everything else is Smart Shuffle)
        self.current_playlist = CustomShuffleAlgorithm.truly_random(self.all_tracks)
        
        # Move currently playing track to position 0
        if currently_playing_track:
            try:
                # Find and remove the currently playing track from the shuffled playlist
                playing_index = next(i for i, t in enumerate(self.current_playlist) 
                                   if self._norm_path(t.filepath) == self._norm_path(currently_playing_track.filepath))
                self.current_playlist.pop(playing_index)
                
                # Insert it at the beginning
                self.current_playlist.insert(0, currently_playing_track)
                self.current_index = 0
            except (StopIteration, ValueError):
                self.current_index = 0
        else:
            self.current_index = 0
        
        # Clear search filter to show full shuffled playlist
        self.filtered_playlist = []
        self.search_var.set('')
        
        self.display_playlist()
        self.status_label.config(text=f"✓ {algo} applied to {len(self.current_playlist)} tracks")
    
    def _apply_shuffle_results(self, playlist, algo):
        """Apply shuffle results on main thread"""
        currently_playing_track = None
        if self.is_playing and self.current_playlist and 0 <= self.current_index < len(self.current_playlist):
            currently_playing_track = self.current_playlist[self.current_index]
        
        # Move currently playing track to position 0
        if currently_playing_track:
            try:
                # Find and remove the currently playing track from the shuffled playlist
                playing_index = next(i for i, t in enumerate(playlist) 
                                   if self._norm_path(t.filepath) == self._norm_path(currently_playing_track.filepath))
                playlist.pop(playing_index)
                
                # Insert it at the beginning
                playlist.insert(0, currently_playing_track)
                self.current_index = 0
            except (StopIteration, ValueError):
                # Track not found in new playlist (shouldn't happen), just use playlist as-is
                self.current_index = 0
        else:
            self.current_index = 0
        
        self.current_playlist = playlist
        self.filtered_playlist = []  # Clear search filter
        self.search_var.set('')  # Clear search box
        self.display_playlist()
        self.status_label.config(text=f"✓ {algo} applied to {len(self.current_playlist)} tracks")
        
        self._set_navigation_enabled(True)
    
    def display_playlist(self):
        """Display playlist in treeview"""
        for item in self.playlist_tree.get_children():
            self.playlist_tree.delete(item)
        
        # Use filtered playlist if search is active, otherwise show full playlist
        display_list = self.filtered_playlist if self.filtered_playlist else self.current_playlist
        
        for i, track in enumerate(display_list, 1):
            self.playlist_tree.insert('', 'end', text=str(i),
                                     values=(track.artist, track.title, track.album,
                                            track.format_duration()))
        
        if self.is_playing and 0 <= self.current_index < len(self.current_playlist):
            # Find currently playing track in the display list
            current_track = self.current_playlist[self.current_index]
            try:
                display_idx = display_list.index(current_track)
                children = self.playlist_tree.get_children()
                if 0 <= display_idx < len(children):
                    item = children[display_idx]
                    self.playlist_tree.selection_set(item)
                    self.playlist_tree.see(item)
            except ValueError:
                # Track not in display list (filtered out)
                pass
    
    def on_track_double_click(self, event):
        """Play track on double-click"""
        selection = self.playlist_tree.selection()
        if selection:
            item = selection[0]
            display_index = int(self.playlist_tree.item(item, 'text')) - 1
            
            # If search is active, map from filtered view to actual playlist
            if self.filtered_playlist:
                if display_index < len(self.filtered_playlist):
                    clicked_track = self.filtered_playlist[display_index]
                    # Find this track in the actual playlist
                    try:
                        actual_index = self.current_playlist.index(clicked_track)
                        self.play_track_at_index(actual_index)
                    except ValueError:
                        pass
            else:
                self.play_track_at_index(display_index)
    
    def _schedule_filter(self):
        """Schedule a debounced search filter update"""
        if self._search_job:
            try:
                self.root.after_cancel(self._search_job)
            except (RuntimeError, KeyError):
                pass  # Job already cancelled
        self._search_job = self.root.after(SEARCH_DEBOUNCE_MS, self._run_filter)
    
    def _run_filter(self):
        """Execute the search filter (called after debounce delay)"""
        self._search_job = None
        self.filter_playlist()
    
    def filter_playlist(self):
        """Filter playlist based on search query - searches active playlist/view"""
        query = self.search_var.get().lower().strip()

        if not query:
            self.filtered_playlist = []
            self.display_playlist()
            if hasattr(self, 'status_label'):
                self.status_label.config(text=f"{len(self.current_playlist)} tracks")
            return

        # Task 2: search within the active playlist/view (not the full library)
        self.filtered_playlist = [
            t for t in self.current_playlist
            if query in t.title.lower()
            or query in t.artist.lower()
            or query in t.album.lower()
            or query in t.genre.lower()
        ]

        self.display_playlist()

        if hasattr(self, 'status_label'):
            self.status_label.config(
                text=f"Showing {len(self.filtered_playlist)} of {len(self.current_playlist)} tracks"
            )
    
    def clear_search(self):
        """Clear search box and show full playlist"""
        self.search_var.set('')
        self.search_entry.focus()
    
    def _play_first_result(self):
        """Play first search result when Enter is pressed"""
        if not self.filtered_playlist:
            return

        first_track = self.filtered_playlist[0]
        target_fp = self._norm_path(first_track.filepath)

        # Task 2: use _norm_path for reliable cross-case/separator comparison
        for i, track in enumerate(self.current_playlist):
            if self._norm_path(getattr(track, 'filepath', '')) == target_fp:
                self.play_track_at_index(i)
                return

        # Fallback: track not found in current playlist (shouldn't happen post-Task 2)
        self.current_playlist = [first_track]
        self.current_index = 0
        self.play_track_at_index(0)
        self.display_playlist()
    
    # Up Next Queue Methods
    
    def _on_playlist_right_click(self, event):
        """Handle right-click on playlist row"""
        try:
            row_id = self.playlist_tree.identify_row(event.y)
            if row_id:
                self.playlist_tree.selection_set(row_id)
                self.playlist_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.playlist_menu.grab_release()
            except (RuntimeError, tk.TclError):
                pass  # Menu already released or window destroyed
    
    def _get_selected_track(self):
        """Get the track object for currently selected Treeview row"""
        sel = self.playlist_tree.selection()
        if not sel:
            return None
        
        item = sel[0]
        try:
            n = int(self.playlist_tree.item(item, 'text')) - 1
        except:
            return None
        
        # Use filtered playlist if search is active
        tracks_to_show = self.filtered_playlist if self.filtered_playlist else self.current_playlist
        
        if 0 <= n < len(tracks_to_show):
            return tracks_to_show[n]
        return None
    
    def _ctx_play_next(self):
        """Context menu: Play Next"""
        track = self._get_selected_track()
        if track:
            self.queue_track(track, play_next=True)
    
    def _ctx_add_up_next(self):
        """Context menu: Add to Up Next"""
        track = self._get_selected_track()
        if track:
            self.queue_track(track, play_next=False)
    
    def queue_track(self, track, play_next=False):
        """Add track to Up Next queue"""
        fp = getattr(track, 'filepath', None)
        if not fp:
            return

        # Task 2: normalise paths for reliable cross-case/separator comparison
        norm_fp = self._norm_path(fp)

        # Don't queue currently playing track
        if 0 <= self.current_index < len(self.current_playlist):
            current_track = self.current_playlist[self.current_index]
            if self._norm_path(getattr(current_track, 'filepath', '')) == norm_fp:
                self.status_label.config(text="Cannot queue currently playing track")
                return

        # Check if already queued (deduplication via normalised path set)
        if norm_fp in self.up_next_set:
            self.status_label.config(text="Already in Up Next")
            return

        if play_next:
            self.up_next.appendleft(track)
            self.status_label.config(text=f"▶ Will play next: {track.title}")
        else:
            self.up_next.append(track)
            self.status_label.config(text=f"Added to Up Next ({len(self.up_next)} queued)")

        self.up_next_set.add(norm_fp)
    
    def view_up_next(self):
        """Show Up Next queue in a dialog"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Up Next Queue")
        dialog.geometry("500x400")
        
        if not self.up_next:
            ttk.Label(dialog, text="Up Next queue is empty", 
                     font=('Arial', 12)).pack(pady=50)
            ttk.Button(dialog, text="Close", command=dialog.destroy).pack()
            return
        
        ttk.Label(dialog, text=f"{len(self.up_next)} tracks in queue:", 
                 font=('Arial', 12, 'bold')).pack(pady=10)
        
        # List of queued tracks
        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        listbox = tk.Listbox(frame, font=('Arial', 10))
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        listbox.config(yscrollcommand=scrollbar.set)
        
        for i, track in enumerate(self.up_next, 1):
            listbox.insert(tk.END, f"{i}. {track.artist} - {track.title}")
        
        # Buttons
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Clear Queue", 
                  command=lambda: (self.clear_up_next(), dialog.destroy())).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Close", command=dialog.destroy).pack(side=tk.LEFT, padx=5)
    
    def clear_up_next(self):
        """Clear Up Next queue"""
        count = len(self.up_next)
        self.up_next.clear()
        self.up_next_set.clear()
        if count > 0:
            self.status_label.config(text=f"Cleared {count} tracks from Up Next")
        else:
            self.status_label.config(text="Up Next queue already empty")
    
    def play_track_at_index(self, index):
        """Play specific track"""
        if not self.audio_available:
            messagebox.showwarning("Audio Unavailable", "Audio device not initialized. Cannot play music.")
            return
        
        if not self.current_playlist or index >= len(self.current_playlist):
            return
        
        self.current_index = index
        track = self.current_playlist[index]
        
        playback_path = track.filepath
        
        if track.filepath.lower().endswith('.m4a'):
            # Stop current playback immediately for instant feedback
            if self.is_playing:
                try:
                    pygame.mixer.music.stop()
                    self.is_playing = False
                except:
                    pass
            
            self.now_playing_label.config(text=track.title)
            self.now_playing_artist.config(text="Converting...")
            self.root.update()
            
            # Capture current track info for validation
            expected_filepath = track.filepath
            expected_index = self.current_index
            
            # Convert in background thread to keep UI responsive
            def convert_and_play():
                converted_path = self._convert_m4a(track.filepath)
                
                def play_converted():
                    # Validate this track is still current before playing
                    if (self.current_index == expected_index and 
                        0 <= self.current_index < len(self.current_playlist) and
                        self.current_playlist[self.current_index].filepath == expected_filepath):
                        
                        if converted_path:
                            self._play_track_direct(track, converted_path)
                        else:
                            print(f"⚠ Skipping unplayable file: {os.path.basename(track.filepath)}")
                            self.next_track(_internal=True)
                    else:
                        print(f"⏭ Conversion completed but track was skipped")
                
                self.root.after(0, play_converted)
            
            threading.Thread(target=convert_and_play, daemon=True).start()
            return
        
        # Non-M4A files: play directly
        self._play_track_direct(track, playback_path)
    
    def _play_track_direct(self, track, playback_path):
        """Actually load and play a track (called after any conversion is done)"""
        try:
            pygame.mixer.music.load(playback_path)
            pygame.mixer.music.play()
            self.is_playing = True
            
            # Log what's playing with file extension
            ext = os.path.splitext(track.filepath)[1]
            
            # V2: Increment play count for ALL tracks (defensive programming)
            track.play_count = (track.play_count or 0) + 1
            track.last_played = int(time.time())

            # Set date_added if not already set (None check preserves 0 and imported values)
            if track.date_added is None:
                track.date_added = int(time.time())

            self._update_track_metadata(track)

            # Task 5: record in recent play history for shuffle history guard
            norm_fp   = self._norm_path(track.filepath)
            artist_k  = (track.artist or "").strip().lower() or None
            self._recent_play_history.append((norm_fp, artist_k))
            print(f"▶ Playing: {track.title}{ext} (plays: {track.play_count})")
            
            self._seek_base_sec = 0.0
            self._segment_start_mono = time.monotonic()
            
            self._load_album_artwork(track)
            
            # Update love button visibility and state
            self._update_love_button(track)
            
            self.now_playing_label.config(text=track.title)
            artist_album = f"{track.artist} - {track.album}" if track.album else track.artist
            self.now_playing_artist.config(text=artist_album)
            
            # Select the row in the CURRENT view (filtered or full), not by playlist index
            try:
                display_list = self.filtered_playlist if self.filtered_playlist else self.current_playlist
                display_idx = display_list.index(track)  # track is self.current_playlist[index]
                children = self.playlist_tree.get_children()
                if 0 <= display_idx < len(children):
                    item = children[display_idx]
                    self.playlist_tree.selection_set(item)
                    self.playlist_tree.see(item)
            except:
                pass
        except pygame.error as e:
            error_msg = str(e).lower()
            filename = os.path.basename(playback_path)
            
            # Concise error for corrupted files
            if any(x in error_msg for x in ['corrupt', 'bad stream', 'invalid', 'drflac']):
                print(f"⚠ Skipping corrupted file: {filename}")
            else:
                print(f"⚠ Playback error: {filename} - {e}")
            
            # Delete corrupt cached conversion if applicable
            if playback_path != track.filepath and os.path.exists(playback_path):
                try:
                    os.remove(playback_path)
                except (OSError, IOError) as err:
                    print(f"  Could not delete corrupt cache: {err}")
            
            # Skip immediately to next track
            self.next_track(_internal=True)
        except Exception as e:
            print(f"⚠ Unexpected playback error: {os.path.basename(playback_path)} - {e}")
            import traceback
            traceback.print_exc()  # Print full traceback for debugging
            self.next_track(_internal=True)
    
    def play(self):
        """Play or resume"""
        if not self.audio_available:
            return
        
        if not self.current_playlist:
            messagebox.showwarning("Warning", "No playlist loaded")
            return
        
        if not pygame.mixer.music.get_busy():
            self.play_track_at_index(self.current_index)
        else:
            # Reset segment start time when unpausing
            self._segment_start_mono = time.monotonic()
            pygame.mixer.music.unpause()
            self.is_playing = True
    
    def pause(self):
        """Pause playback"""
        if not self.audio_available:
            return
        
        # Capture current position before pausing
        if self.is_playing and self._segment_start_mono is not None:
            elapsed = time.monotonic() - self._segment_start_mono
            self._seek_base_sec = self._seek_base_sec + elapsed
        
        pygame.mixer.music.pause()
        self.is_playing = False
    
    def stop(self):
        """Stop playback"""
        if not self.audio_available:
            return
        
        pygame.mixer.music.stop()
        self.is_playing = False
        self.now_playing_label.config(text="Not Playing")
        self.now_playing_artist.config(text="")
        self._show_placeholder_artwork()
        self.progress_var.set(0)
        self.time_label.config(text="0:00 / 0:00")
    
    def next_track(self, _internal=False):
        """Play next track"""
        if not self.current_playlist:
            return
        
        if (not _internal and hasattr(self, 'next_button') 
                and str(self.next_button['state']) == 'disabled'):
            print("⚠ Navigation disabled during shuffle")
            return
        
        # V2: Track skip count only if user manually skipped AND track was less than 80% complete
        if not _internal and self.is_playing and 0 <= self.current_index < len(self.current_playlist):
            track = self.current_playlist[self.current_index]
            
            # Calculate completion percentage
            should_count_skip = True
            duration = self._get_safe_duration(track)
            if duration > 0.01 and self._segment_start_mono is not None:
                try:
                    pos = self._seek_base_sec + (time.monotonic() - self._segment_start_mono)
                    completion_pct = pos / duration
                    # Only count as skip if less than threshold complete
                    if completion_pct >= SKIP_COMPLETION_THRESHOLD:
                        should_count_skip = False
                        print(f"⏭ Near end, not counting as skip: {track.title} ({completion_pct*100:.0f}% complete)")
                except Exception:
                    # If calculation fails, default to counting it as a skip
                    pass
            
            if should_count_skip:
                try:
                    track.skips = int(track.skips or 0) + 1
                except (ValueError, TypeError):
                    track.skips = 1
                self._update_track_metadata(track)
                print(f"⏭ Skipped: {track.title} (skips: {track.skips})")
        
        # Up Next queue takes priority
        if self.up_next:
            next_track = self.up_next.popleft()
            fp = getattr(next_track, 'filepath', None)
            if fp:
                norm_fp = self._norm_path(fp)
                self.up_next_set.discard(norm_fp)
            
            # Try to find track in current playlist
            idx = None
            norm_fp = self._norm_path(fp)
            for i, t in enumerate(self.current_playlist):
                track_fp = getattr(t, 'filepath', None)
                if track_fp and self._norm_path(track_fp) == norm_fp:
                    idx = i
                    break
            
            if idx is not None:
                # Track is in current playlist - play it
                self.current_index = idx
                self.play_track_at_index(self.current_index)
                print(f"▶ Playing from Up Next: {next_track.title}")
            else:
                # Track not in current playlist - add it temporarily
                self.current_playlist.insert(self.current_index + 1, next_track)
                self.current_index += 1
                self.play_track_at_index(self.current_index)
                print(f"▶ Playing from Up Next: {next_track.title}")
                self.display_playlist()
            return
        
        # Normal next track behavior
        self.current_index = (self.current_index + 1) % len(self.current_playlist)
        self.play_track_at_index(self.current_index)
    
    def previous_track(self):
        """Play previous track"""
        if not self.current_playlist:
            return
        
        if hasattr(self, 'prev_button') and str(self.prev_button['state']) == 'disabled':
            print("⚠ Navigation disabled during shuffle")
            return
        
        self.current_index = (self.current_index - 1) % len(self.current_playlist)
        self.play_track_at_index(self.current_index)
    
    def start_update_thread(self):
        """Start thread to update progress bar and sync volume"""
        def safe_ui_call(callback, *args):
            """Safely call UI callback, checking if window still exists"""
            try:
                if self.root.winfo_exists():
                    self.root.after(0, callback, *args)
            except (RuntimeError, tk.TclError):
                pass  # Window destroyed or Tcl interpreter deleted
        
        def update_progress():
            _tick = 0.1  # seconds per loop iteration
            _volume_sync_ticks = round(VOLUME_SYNC_INTERVAL_SEC / _tick)

            last_pos = 0
            end_detected = False
            volume_sync_counter = 0

            while True:
                # Sync volume slider with system volume every VOLUME_SYNC_INTERVAL_SEC
                volume_sync_counter += 1
                if volume_sync_counter >= _volume_sync_ticks:
                    volume_sync_counter = 0
                    safe_ui_call(self._sync_volume_slider)
                
                if self.audio_available and self.is_playing:
                    try:
                        if self.current_playlist and pygame.mixer.music.get_busy():
                            track = self.current_playlist[self.current_index]
                            
                            if self._segment_start_mono is None:
                                self._segment_start_mono = time.monotonic()
                            
                            pos = self._seek_base_sec + (time.monotonic() - self._segment_start_mono)
                            
                            if pos < 0:
                                pos = 0.0
                            
                            # Reset end detection if position is clearly not at end
                            if pos > 0.2:
                                end_detected = False
                            
                            if pos < last_pos - 1.0:
                                end_detected = False
                            last_pos = pos
                            
                            duration = self._get_safe_duration(track)
                            if duration > 0.01:
                                # Calculate progress - allow slightly over 100% but clamp for display
                                progress = (pos / duration) * 100
                                
                                # For time display, clamp position to duration to prevent showing "3:45 / 3:30"
                                display_pos = min(pos, duration)
                                pos_min = int(display_pos // 60)
                                pos_sec = int(display_pos % 60)
                                dur_min = int(duration // 60)
                                dur_sec = int(duration % 60)
                                time_text = f"{pos_min}:{pos_sec:02d} / {dur_min}:{dur_sec:02d}"
                                
                                # Cap progress bar but allow it to reach 100% when track actually ends
                                # Don't cap at 99.5% - this was causing confusion
                                if progress > 100:
                                    progress = 100
                                
                                safe_ui_call(self._update_progress_ui, progress, time_text)
                        
                        elif not pygame.mixer.music.get_busy() and not end_detected:
                            # Verify track actually ended (not just a momentary glitch)
                            # Check if position is actually near the end
                            if self.current_playlist and self.current_index >= 0:
                                track = self.current_playlist[self.current_index]
                                
                                # Skip if timestamp tracking not initialized
                                if self._segment_start_mono is None:
                                    # Track never started properly, trust get_busy()
                                    end_detected = True
                                    last_pos = 0
                                    safe_ui_call(self._handle_track_end)
                                    continue
                                
                                pos = self._seek_base_sec + (time.monotonic() - self._segment_start_mono)
                                
                                # Only trigger end if we're actually near the end of the track
                                # Dynamic buffer: 10% of track or 2 seconds, whichever is less
                                # This prevents short tracks (<2s) from never ending
                                if track.duration > 0:
                                    buffer = min(track.duration * 0.1, END_DETECTION_BUFFER_SEC)
                                    if pos >= (track.duration - buffer):
                                        end_detected = True
                                        last_pos = 0
                                        safe_ui_call(self._handle_track_end)
                                elif track.duration <= 0:
                                    # Unknown duration - trust get_busy()
                                    end_detected = True
                                    last_pos = 0
                                    safe_ui_call(self._handle_track_end)
                                # else: not at end yet, might be buffering, keep playing
                    
                    except pygame.error:
                        # pygame errors are common during track transitions
                        pass
                    except Exception as e:
                        # Log unexpected errors but don't crash the thread
                        print(f"⚠ Progress update error: {e}")
                        time.sleep(1)  # Back off briefly before retrying

                time.sleep(_tick)
        
        thread = threading.Thread(target=update_progress, daemon=True)
        thread.start()
    
    def _update_progress_ui(self, progress, time_text):
        """Update progress bar and time label"""
        try:
            self.progress_var.set(min(progress, 100))
            self.time_label.config(text=time_text)
        except:
            pass
    
    def _handle_track_end(self):
        """Handle track ending"""
        if self.is_playing and not pygame.mixer.music.get_busy():
            # V2: Track completed naturally (ALL tracks)
            if 0 <= self.current_index < len(self.current_playlist):
                track = self.current_playlist[self.current_index]
                print(f"✓ Completed: {track.title}")
            
            self.is_playing = False
            self.next_track(_internal=True)  # Allow auto-advance even during shuffle


def main():
    print("="*70)
    print("Custom Shuffle Music Player V2 - Starting...")
    print("Unified metadata system - all tracks tracked equally")
    print("Console visible for debugging - errors will be shown here")
    print("="*70)
    print()
    
    # Minimize console window on Windows
    if sys.platform == 'win32':
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 2)  # SW_SHOWMINIMIZED = 2
        except:
            pass
    
    try:
        root = tk.Tk()
        app = MusicPlayerGUI(root)
        root.mainloop()
        print("\n" + "="*70)
        print("App closed normally")
        print("="*70)
    except Exception as e:
        print("\n" + "="*70)
        print("CRITICAL ERROR - APP CRASHED")
        print("="*70)
        import traceback
        traceback.print_exc()
        print("="*70)
        input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()