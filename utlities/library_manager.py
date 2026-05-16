#!/usr/bin/env python3
"""iTunes Library Manager — manage track order and metadata in your iTunes library."""

import sys
import os
import plistlib
from pathlib import Path
from urllib.parse import unquote, urlparse
from datetime import datetime
from dataclasses import dataclass
from collections import defaultdict
import io
import base64

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter,
    QListWidget, QListWidgetItem, QAbstractItemView,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QPushButton, QLineEdit, QLabel, QStatusBar,
    QDialog, QDialogButtonBox, QFormLayout,
    QVBoxLayout, QHBoxLayout, QSpinBox, QTextEdit,
    QFileDialog, QMessageBox, QSizePolicy,
)
from PyQt6.QtCore import Qt, QSize, QThread, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QPixmap, QColor, QPainter, QFont, QIcon

from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.id3 import (
    TIT2, TPE1, TALB, TPE2, TDRC, TCON, TCOM, COMM,
    TRCK, TPOS, TBPM, APIC, ID3NoHeaderError,
)
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC, Picture as FLACPicture
from mutagen.oggvorbis import OggVorbis
from PIL import Image

import win32com.client
import pywintypes


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class TrackInfo:
    title: str
    artist: str
    album: str
    album_artist: str
    year: str
    genre: str
    composer: str
    comment: str
    track_number: int       # current value from XML/file
    disc_number: int        # 0 = single-disc/unknown
    bpm: int
    rating: int             # 0–100 iTunes scale
    date_added: datetime | None
    file_path: str          # normalized Windows path
    track_total: int = 0    # 0 = unknown
    disc_total: int = 0     # 0 = unknown
    compilation: bool = False


@dataclass
class AlbumInfo:
    key: tuple[str, str]    # (album_artist_or_artist_lower, album_lower)
    album_name: str
    display_artist: str     # album_artist if set, else artist
    first_track_path: str   # for artwork extraction
    track_count: int


class DRMError(Exception):
    pass


def _int_or_zero(value) -> int:
    if value in (None, ''):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _album_identity(
    artist: str,
    album_artist: str,
    album: str,
    compilation: bool = False,
) -> tuple[tuple[str, str], str, str]:
    album_name = album or 'Unknown Album'
    group_artist = 'Various Artists' if compilation else (album_artist or artist)
    display_artist = album_artist or ('Various Artists' if compilation else artist)
    return (group_artist.lower(), album_name.lower()), album_name, display_artist


def _album_key_for_track(track: TrackInfo) -> tuple[str, str]:
    key, _, _ = _album_identity(
        track.artist, track.album_artist, track.album, track.compilation
    )
    return key


# ── iTunesXMLReader ───────────────────────────────────────────────────────────

class iTunesXMLReader:
    _XML_CANDIDATES = [
        Path.home() / 'Music' / 'iTunes' / 'iTunes Library.xml',
        Path.home() / 'Music' / 'iTunes' / 'iTunes Music Library.xml',
    ]

    def find_xml(self) -> Path:
        for p in self._XML_CANDIDATES:
            if p.exists():
                return p
        raise FileNotFoundError(
            "iTunes Library XML not found.\n\n"
            "To enable it: iTunes → Edit → Preferences → Advanced → "
            "Share iTunes Library XML with other applications\n\n"
            "Searched:\n" + "\n".join(f"  {p}" for p in self._XML_CANDIDATES)
        )

    def load(self) -> tuple[list[AlbumInfo], dict[tuple[str, str], list[TrackInfo]]]:
        xml_path = self.find_xml()
        with open(xml_path, 'rb') as f:
            library = plistlib.load(f)

        raw_tracks = library.get('Tracks', {})
        by_album: dict[tuple[str, str], list[TrackInfo]] = defaultdict(list)
        album_display: dict[tuple[str, str], dict] = {}

        for track_id, t in raw_tracks.items():
            location = t.get('Location', '')
            if not location:
                continue
            try:
                parsed = urlparse(location)
                path = unquote(parsed.path)
                if parsed.netloc and parsed.netloc.lower() != 'localhost':
                    # UNC: file://NAS/Music/... → \\NAS\Music\...
                    path = '//' + parsed.netloc + path
                elif path.startswith('/') and ':' in path:
                    path = path[1:]
                path = os.path.normcase(os.path.normpath(path))

                artist = t.get('Artist', '') or ''
                album_artist = t.get('Album Artist', '') or ''
                album = t.get('Album', '') or ''
                compilation = bool(t.get('Compilation'))
                key, album_name, display_artist = _album_identity(
                    artist, album_artist, album, compilation
                )

                date_added = t.get('Date Added')
                track = TrackInfo(
                    title=t.get('Name', '') or '',
                    artist=artist,
                    album=album,
                    album_artist=album_artist,
                    year=str(t.get('Year', '')) if t.get('Year') else '',
                    genre=t.get('Genre', '') or '',
                    composer=t.get('Composer', '') or '',
                    comment=t.get('Comments', '') or '',
                    track_number=_int_or_zero(t.get('Track Number')),
                    disc_number=_int_or_zero(t.get('Disc Number')),
                    bpm=_int_or_zero(t.get('BPM')),
                    rating=_int_or_zero(t.get('Rating')),
                    date_added=date_added if isinstance(date_added, datetime) else None,
                    file_path=path,
                    track_total=_int_or_zero(t.get('Track Count')),
                    disc_total=_int_or_zero(t.get('Disc Count')),
                    compilation=compilation,
                )
                by_album[key].append(track)
                if key not in album_display:
                    album_display[key] = {
                        'album_name': album_name,
                        'display_artist': display_artist,
                    }
            except Exception as e:
                print(f'[library_manager] XML: skipped track {track_id}: {e}', file=sys.stderr)
                continue

        tracks_by_album: dict[tuple[str, str], list[TrackInfo]] = {}
        album_list: list[AlbumInfo] = []

        for key, tracks in by_album.items():
            sorted_tracks = sorted(
                tracks, key=lambda tr: tr.date_added or datetime.min
            )
            tracks_by_album[key] = sorted_tracks
            meta = album_display[key]
            album_list.append(AlbumInfo(
                key=key,
                album_name=meta['album_name'],
                display_artist=meta['display_artist'],
                first_track_path=sorted_tracks[0].file_path,
                track_count=len(sorted_tracks),
            ))

        album_list.sort(key=lambda a: a.album_name.lower())
        return album_list, tracks_by_album


# ── MetadataWriter ────────────────────────────────────────────────────────────

class MetadataWriter:
    """Writes audio file tags using mutagen. Rating is NOT written here (COM-only)."""

    @staticmethod
    def _image_mime(data: bytes) -> str:
        if data[:3] == b'\xff\xd8\xff':
            return 'image/jpeg'
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return 'image/png'
        return 'image/jpeg'

    @staticmethod
    def _mp4_cover_format(data: bytes) -> int:
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return MP4Cover.FORMAT_PNG
        return MP4Cover.FORMAT_JPEG

    def write_all(self, track: TrackInfo, artwork_bytes: bytes | None = None) -> list[str]:
        """Write all metadata fields. Returns list of skipped-field messages."""
        ext = Path(track.file_path).suffix.lower()
        if ext == '.m4p':
            raise DRMError(f"DRM-protected: {track.file_path}")
        try:
            if ext == '.mp3':
                return self._write_mp3(track, artwork_bytes)
            elif ext in ('.m4a', '.m4b', '.mp4'):
                return self._write_mp4(track, artwork_bytes)
            elif ext == '.flac':
                return self._write_flac(track, artwork_bytes)
            elif ext == '.ogg':
                return self._write_ogg(track, artwork_bytes)
            elif ext == '.aac':
                try:
                    return self._write_aac(track)
                except Exception as e:
                    return [f"AAC write failed: {e}"]
            return [f"Unsupported format: {ext}"]
        except FileNotFoundError:
            return [f"File not found: {track.file_path}"]
        except Exception as e:
            return [f"Write failed ({ext}): {e}"]

    def write_track_number(self, track: TrackInfo) -> list[str]:
        """Write only the track number field."""
        ext = Path(track.file_path).suffix.lower()
        if ext == '.m4p':
            raise DRMError(f"DRM-protected: {track.file_path}")
        try:
            if ext == '.mp3':
                audio = MP3(track.file_path)
                if audio.tags is None:
                    audio.add_tags()
                trck_val = f'{track.track_number}/{track.track_total}' if track.track_total else str(track.track_number)
                audio.tags['TRCK'] = TRCK(encoding=3, text=[trck_val])
                audio.save()
            elif ext in ('.m4a', '.m4b', '.mp4'):
                audio = MP4(track.file_path)
                existing = audio.get('trkn', [(0, 0)])
                file_total = existing[0][1] if existing else 0
                total = track.track_total or file_total
                audio['trkn'] = [(track.track_number, total)]
                audio.save()
            elif ext == '.flac':
                audio = FLAC(track.file_path)
                audio['TRACKNUMBER'] = [str(track.track_number)]
                if track.track_total:
                    audio['TRACKTOTAL'] = [str(track.track_total)]
                elif 'TRACKTOTAL' in audio:
                    del audio['TRACKTOTAL']
                audio.save()
            elif ext == '.ogg':
                audio = OggVorbis(track.file_path)
                audio['tracknumber'] = [str(track.track_number)]
                if track.track_total:
                    audio['tracktotal'] = [str(track.track_total)]
                elif 'tracktotal' in audio:
                    del audio['tracktotal']
                audio.save()
            elif ext == '.aac':
                return ['AAC: raw AAC does not support tag writes — track number not written']
            else:
                return [f"Track number not written: unsupported format {ext}"]
        except FileNotFoundError:
            return [f"File not found: {track.file_path}"]
        except Exception as e:
            return [f"Track number write failed: {e}"]
        return []

    # ── Format-specific writers ──

    def _write_mp3(self, track: TrackInfo, artwork_bytes: bytes | None) -> list[str]:
        audio = MP3(track.file_path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        tags['TIT2'] = TIT2(encoding=3, text=[track.title])
        tags['TPE1'] = TPE1(encoding=3, text=[track.artist])
        tags['TALB'] = TALB(encoding=3, text=[track.album])
        tags['TPE2'] = TPE2(encoding=3, text=[track.album_artist])
        tags['TDRC'] = TDRC(encoding=3, text=[track.year])
        tags['TCON'] = TCON(encoding=3, text=[track.genre])
        tags['TCOM'] = TCOM(encoding=3, text=[track.composer])
        tags['COMM'] = COMM(encoding=3, lang='eng', desc='', text=[track.comment])
        trck_val = f'{track.track_number}/{track.track_total}' if track.track_total else str(track.track_number)
        tags['TRCK'] = TRCK(encoding=3, text=[trck_val])
        if track.disc_number:
            tpos_val = f'{track.disc_number}/{track.disc_total}' if track.disc_total else str(track.disc_number)
            tags['TPOS'] = TPOS(encoding=3, text=[tpos_val])
        else:
            tags.delall('TPOS')
        if track.bpm:
            tags['TBPM'] = TBPM(encoding=3, text=[str(track.bpm)])
        else:
            tags.delall('TBPM')
        if artwork_bytes:
            tags['APIC'] = APIC(
                encoding=3, mime=self._image_mime(artwork_bytes), type=3,
                desc='Cover', data=artwork_bytes,
            )
        audio.save()
        return []

    def _write_mp4(self, track: TrackInfo, artwork_bytes: bytes | None) -> list[str]:
        audio = MP4(track.file_path)
        audio['©nam'] = [track.title]
        audio['©ART'] = [track.artist]
        audio['©alb'] = [track.album]
        audio['aART'] = [track.album_artist]
        audio['©day'] = [track.year]
        audio['©gen'] = [track.genre]
        audio['©wrt'] = [track.composer]
        audio['©cmt'] = [track.comment]
        existing_trkn = audio.get('trkn', [(0, 0)])
        file_trkn_total = existing_trkn[0][1] if existing_trkn else 0
        audio['trkn'] = [(track.track_number, track.track_total or file_trkn_total)]
        if track.disc_number:
            existing_disk = audio.get('disk', [(0, 0)])
            file_disk_total = existing_disk[0][1] if existing_disk else 0
            audio['disk'] = [(track.disc_number, track.disc_total or file_disk_total)]
        else:
            audio.pop('disk', None)
        if track.bpm:
            audio['tmpo'] = [track.bpm]
        else:
            audio.pop('tmpo', None)
        if artwork_bytes:
            audio['covr'] = [MP4Cover(artwork_bytes, imageformat=self._mp4_cover_format(artwork_bytes))]
        audio.save()
        return []

    def _write_flac(self, track: TrackInfo, artwork_bytes: bytes | None) -> list[str]:
        audio = FLAC(track.file_path)
        audio['TITLE'] = [track.title]
        audio['ARTIST'] = [track.artist]
        audio['ALBUM'] = [track.album]
        audio['ALBUMARTIST'] = [track.album_artist]
        audio['DATE'] = [track.year]
        audio['GENRE'] = [track.genre]
        audio['COMPOSER'] = [track.composer]
        audio['COMMENT'] = [track.comment]
        audio['TRACKNUMBER'] = [str(track.track_number)]
        if track.track_total:
            audio['TRACKTOTAL'] = [str(track.track_total)]
        elif 'TRACKTOTAL' in audio:
            del audio['TRACKTOTAL']
        if track.disc_number:
            audio['DISCNUMBER'] = [str(track.disc_number)]
            if track.disc_total:
                audio['DISCTOTAL'] = [str(track.disc_total)]
            elif 'DISCTOTAL' in audio:
                del audio['DISCTOTAL']
        elif 'DISCNUMBER' in audio:
            del audio['DISCNUMBER']
            if 'DISCTOTAL' in audio:
                del audio['DISCTOTAL']
        if track.bpm:
            audio['BPM'] = [str(track.bpm)]
        elif 'BPM' in audio:
            del audio['BPM']
        if artwork_bytes:
            pic = FLACPicture()
            pic.type = 3
            pic.mime = self._image_mime(artwork_bytes)
            pic.data = artwork_bytes
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()
        return []

    def _write_ogg(self, track: TrackInfo, artwork_bytes: bytes | None) -> list[str]:
        audio = OggVorbis(track.file_path)
        audio['title'] = [track.title]
        audio['artist'] = [track.artist]
        audio['album'] = [track.album]
        audio['albumartist'] = [track.album_artist]
        audio['date'] = [track.year]
        audio['genre'] = [track.genre]
        audio['composer'] = [track.composer]
        audio['comment'] = [track.comment]
        audio['tracknumber'] = [str(track.track_number)]
        if track.track_total:
            audio['tracktotal'] = [str(track.track_total)]
        elif 'tracktotal' in audio:
            del audio['tracktotal']
        if track.disc_number:
            audio['discnumber'] = [str(track.disc_number)]
            if track.disc_total:
                audio['disctotal'] = [str(track.disc_total)]
            elif 'disctotal' in audio:
                del audio['disctotal']
        elif 'discnumber' in audio:
            del audio['discnumber']
            if 'disctotal' in audio:
                del audio['disctotal']
        if track.bpm:
            audio['bpm'] = [str(track.bpm)]
        elif 'bpm' in audio:
            del audio['bpm']
        if artwork_bytes:
            pic = FLACPicture()
            pic.type = 3
            pic.mime = self._image_mime(artwork_bytes)
            pic.data = artwork_bytes
            pic.width = pic.height = pic.depth = pic.colors = 0
            audio['metadata_block_picture'] = [
                base64.b64encode(pic.write()).decode('ascii')
            ]
        else:
            audio.pop('metadata_block_picture', None)
        audio.save()
        return []

    def _write_aac(self, track: TrackInfo) -> list[str]:
        return [f'AAC: raw AAC does not support metadata field writes — {Path(track.file_path).name}']


# ── iTunesCOMRefresher ────────────────────────────────────────────────────────

class iTunesCOMRefresher:
    """Syncs tag changes to a running iTunes instance via COM."""

    def __init__(self):
        self._itunes = None
        self._path_cache: dict[str, object] = {}  # normcase(normpath) → COM track

    def connect(self) -> None:
        # GetActiveObject requires iTunes to be in the ROT (fails for Store installs).
        # Dispatch falls back to the COM class factory, which works in both cases.
        # iTunes.Application is a singleton so Dispatch won't launch a second instance.
        try:
            self._itunes = win32com.client.GetActiveObject('iTunes.Application')
        except pywintypes.com_error:
            try:
                self._itunes = win32com.client.Dispatch('iTunes.Application')
            except pywintypes.com_error as e:
                raise RuntimeError(
                    f'Cannot connect to iTunes (COM: {e}). '
                    'Make sure iTunes is open and try again.'
                )
        self._path_cache = {}
        tracks = self._itunes.LibraryPlaylist.Tracks
        count = tracks.Count
        for i in range(1, count + 1):
            try:
                track = tracks.Item(i)
                loc = getattr(track, 'Location', None)
                if loc:
                    norm = os.path.normcase(os.path.normpath(loc))
                    self._path_cache[norm] = track
            except Exception as e:
                print(f'[library_manager] COM cache: skipped track {i}: {e}', file=sys.stderr)
                continue

    def disconnect(self) -> None:
        """Release the COM reference so iTunes can quit without a scripting warning."""
        self._itunes = None
        self._path_cache = {}

    def refresh(
        self,
        file_paths: list[str],
        ratings: dict[str, int] | None = None,
    ) -> list[str]:
        """Refresh changed tracks in iTunes. Returns list of error strings."""
        if self._itunes is None:
            self.connect()

        errors: list[str] = []
        norm_ratings = (
            {os.path.normcase(os.path.normpath(k)): v for k, v in ratings.items()}
            if ratings else {}
        )
        for path in file_paths:
            norm = os.path.normcase(os.path.normpath(path))
            track = self._path_cache.get(norm)
            if track is None:
                errors.append(f'Not in iTunes cache: {Path(path).name}')
                continue
            try:
                track.UpdateInfoFromFile()
                if norm in norm_ratings:
                    track.Rating = norm_ratings[norm]
            except Exception as e:
                errors.append(f'Refresh failed for {Path(path).name}: {e}')
        return errors


# ── Artwork helpers ───────────────────────────────────────────────────────────

def _extract_artwork_bytes(file_path: str) -> bytes | None:
    """Return raw image bytes from the first embedded artwork in an audio file."""
    try:
        audio = MutagenFile(file_path)
        if audio is None:
            return None
        # MP3
        if hasattr(audio, 'tags') and audio.tags:
            for key in audio.tags:
                if key.startswith('APIC'):
                    return audio.tags[key].data
        # MP4
        covers = (audio.tags or {}).get('covr')
        if covers:
            return bytes(covers[0])
        # OGG/Vorbis
        pictures = (audio.tags or {}).get('metadata_block_picture')
        if pictures:
            for encoded_picture in pictures:
                try:
                    if isinstance(encoded_picture, bytes):
                        encoded_picture = encoded_picture.decode('ascii')
                    picture = FLACPicture(base64.b64decode(encoded_picture))
                    return picture.data
                except Exception:
                    continue
        # FLAC
        if hasattr(audio, 'pictures') and audio.pictures:
            return audio.pictures[0].data
    except Exception:
        return None
    return None


def _pixmap_from_bytes(image_bytes: bytes, size: int) -> QPixmap:
    pixmap = QPixmap()
    pixmap.loadFromData(image_bytes)
    if pixmap.isNull():
        return _placeholder_pixmap(size)
    return pixmap.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _placeholder_pixmap(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(50, 50, 65))
    painter = QPainter(pixmap)
    painter.setPen(QColor(160, 160, 180))
    painter.setFont(QFont('Arial', size // 3))
    painter.drawText(
        pixmap.rect(), Qt.AlignmentFlag.AlignCenter, '♪'
    )
    painter.end()
    return pixmap


# ── DraggableTable ────────────────────────────────────────────────────────────

class DraggableTable(QTableWidget):
    """QTableWidget with correct single-row internal drag-drop."""

    def __init__(self):
        super().__init__()
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setDragDropOverwriteMode(False)
        self.verticalHeader().hide()

    def dropEvent(self, event):
        src_row = self.currentRow()
        dst_index = self.indexAt(event.position().toPoint())
        # Invalid index means dropped past last row — use rowCount() so the
        # subsequent decrement lands on the true last position after removal.
        dst_row = dst_index.row() if dst_index.isValid() else self.rowCount()

        if src_row < 0 or src_row == dst_row:
            event.ignore()
            return

        row_items = [self.takeItem(src_row, col) for col in range(self.columnCount())]
        self.removeRow(src_row)

        if dst_row > src_row:
            dst_row -= 1

        self.insertRow(dst_row)
        for col, item in enumerate(row_items):
            self.setItem(dst_row, col, item)

        self.selectRow(dst_row)
        event.accept()


# ── RatingWidget ──────────────────────────────────────────────────────────────

class RatingWidget(QWidget):
    """Clickable 1–5 star widget. Stores rating as 0–100 iTunes scale."""

    def __init__(self, rating_0_100: int = 0, parent=None):
        super().__init__(parent)
        self._stars = round(rating_0_100 / 20)  # 0-100 → 0-5
        self.setFixedSize(130, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setFont(QFont('Arial', 16))
        for i in range(5):
            painter.setPen(
                QColor(255, 200, 0) if i < self._stars else QColor(160, 160, 160)
            )
            painter.drawText(i * 26, 22, '★' if i < self._stars else '☆')

    def mousePressEvent(self, event):
        clicked = int(event.position().x() / 26) + 1
        self._stars = clicked if clicked != self._stars else 0
        self.update()

    def rating(self) -> int:
        return self._stars * 20


# ── MetadataDialog ────────────────────────────────────────────────────────────

class MetadataDialog(QDialog):

    def __init__(self, track: TrackInfo, parent=None):
        super().__init__(parent)
        self._track = track
        self._new_artwork_bytes: bytes | None = None
        self.setWindowTitle(f'Edit Metadata — {track.title}')
        self.resize(480, 640)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)

        self._f_title = QLineEdit(self._track.title)
        self._f_artist = QLineEdit(self._track.artist)
        self._f_album = QLineEdit(self._track.album)
        self._f_album_artist = QLineEdit(self._track.album_artist)
        self._f_year = QLineEdit(self._track.year)
        self._f_genre = QLineEdit(self._track.genre)
        self._f_composer = QLineEdit(self._track.composer)
        self._f_comment = QTextEdit(self._track.comment)
        self._f_comment.setFixedHeight(64)
        self._f_track_num = QSpinBox()
        self._f_track_num.setRange(0, 9999)
        self._f_track_num.setValue(self._track.track_number)
        self._f_disc_num = QSpinBox()
        self._f_disc_num.setRange(0, 99)
        self._f_disc_num.setValue(self._track.disc_number)
        self._f_bpm = QSpinBox()
        self._f_bpm.setRange(0, 999)
        self._f_bpm.setValue(self._track.bpm)
        self._f_rating = RatingWidget(self._track.rating)

        form.addRow('Title:', self._f_title)
        form.addRow('Artist:', self._f_artist)
        form.addRow('Album:', self._f_album)
        form.addRow('Album Artist:', self._f_album_artist)
        form.addRow('Year:', self._f_year)
        form.addRow('Genre:', self._f_genre)
        form.addRow('Composer:', self._f_composer)
        form.addRow('Comment:', self._f_comment)
        form.addRow('Track #:', self._f_track_num)
        form.addRow('Disc #:', self._f_disc_num)
        form.addRow('BPM:', self._f_bpm)
        form.addRow('Rating:', self._f_rating)

        # Artwork row
        art_row = QHBoxLayout()
        self._art_label = QLabel()
        self._art_label.setFixedSize(120, 120)
        self._art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._art_label.setStyleSheet('border: 1px solid #555;')
        self._load_artwork_preview()

        change_btn = QPushButton('Change…')
        change_btn.setFixedWidth(80)
        change_btn.clicked.connect(self._choose_artwork)
        art_row.addWidget(self._art_label)
        art_row.addWidget(change_btn)
        art_row.addStretch()
        form.addRow('Artwork:', art_row)  # type: ignore[arg-type]

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_artwork_preview(self):
        image_bytes = _extract_artwork_bytes(self._track.file_path)
        if image_bytes:
            pixmap = _pixmap_from_bytes(image_bytes, 120)
        else:
            pixmap = _placeholder_pixmap(120)
        self._art_label.setPixmap(pixmap)

    def _choose_artwork(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select Artwork', '',
            'Images (*.jpg *.jpeg *.png *.webp)',
        )
        if not path:
            return
        if path.lower().endswith('.webp'):
            with Image.open(path) as img:
                buf = io.BytesIO()
                img.convert('RGB').save(buf, format='JPEG', quality=90)
                self._new_artwork_bytes = buf.getvalue()
        else:
            self._new_artwork_bytes = Path(path).read_bytes()
        pixmap = QPixmap()
        pixmap.loadFromData(self._new_artwork_bytes)
        if pixmap.isNull():
            pixmap = _placeholder_pixmap(120)
        else:
            pixmap = pixmap.scaled(
                120, 120,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._art_label.setPixmap(pixmap)

    def updated_track_and_artwork(self) -> tuple[TrackInfo, bytes | None]:
        updated = TrackInfo(
            title=self._f_title.text(),
            artist=self._f_artist.text(),
            album=self._f_album.text(),
            album_artist=self._f_album_artist.text(),
            year=self._f_year.text(),
            genre=self._f_genre.text(),
            composer=self._f_composer.text(),
            comment=self._f_comment.toPlainText(),
            track_number=self._f_track_num.value(),
            disc_number=self._f_disc_num.value(),
            bpm=self._f_bpm.value(),
            rating=self._f_rating.rating(),
            date_added=self._track.date_added,
            file_path=self._track.file_path,
            track_total=self._track.track_total,
            disc_total=self._track.disc_total,
            compilation=self._track.compilation,
        )
        return updated, self._new_artwork_bytes


# ── ArtworkThread ─────────────────────────────────────────────────────────────

class ArtworkThread(QThread):
    """Background thread that loads raw artwork bytes for a batch of albums.
    Emits bytes (not QPixmap) — QPixmap must be built in the main thread."""

    artwork_ready = pyqtSignal(str, bytes)  # album_key_str → raw image bytes (empty = no art)

    def __init__(self, queue: list[tuple[str, str]]):
        super().__init__()
        self._queue = queue   # [(key_str, first_track_path), ...]
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        for key_str, track_path in self._queue:
            if self._stopped:
                break
            image_bytes = _extract_artwork_bytes(track_path) or b''
            self.artwork_ready.emit(key_str, image_bytes)


# ── LibraryManagerApp ─────────────────────────────────────────────────────────

class LibraryManagerApp(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle('iTunes Library Manager')
        self.resize(1100, 720)

        self._reader = iTunesXMLReader()
        self._writer = MetadataWriter()
        self._refresher = iTunesCOMRefresher()

        self._albums: list[AlbumInfo] = []
        self._tracks_by_album: dict[tuple[str, str], list[TrackInfo]] = {}
        self._current_key: tuple[str, str] | None = None

        # Maps album key string → QListWidgetItem (for artwork updates)
        self._album_items: dict[str, QListWidgetItem] = {}

        self._build_ui()
        self._load_library()

    # ── UI construction ──

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left panel — album list
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)

        self._search = QLineEdit()
        self._search.setPlaceholderText('Search albums…')
        self._search.textChanged.connect(self._filter_albums)

        self._album_list = QListWidget()
        self._album_list.setIconSize(QSize(60, 60))
        self._album_list.currentItemChanged.connect(self._on_album_selected)

        left_layout.addWidget(self._search)
        left_layout.addWidget(self._album_list)

        # Right panel — song table + buttons
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 4)

        btn_row = QHBoxLayout()
        self._sort_btn = QPushButton('Sort by Date Added')
        self._apply_btn = QPushButton('Apply Track Numbers')
        self._sort_btn.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self._sort_btn.clicked.connect(self._sort_by_date_added)
        self._apply_btn.clicked.connect(self._apply_track_numbers)
        btn_row.addWidget(self._sort_btn)
        btn_row.addWidget(self._apply_btn)
        btn_row.addStretch()

        self._song_table = DraggableTable()
        self._song_table.setColumnCount(5)
        self._song_table.setHorizontalHeaderLabels(
            ['#', 'Title', 'Artist', 'Date Added', 'Disc']
        )
        hdr = self._song_table.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._song_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._song_table.doubleClicked.connect(self._open_metadata_dialog)

        right_layout.addLayout(btn_row)
        right_layout.addWidget(self._song_table)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([280, 820])

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(splitter)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage('Loading library…')

    # ── Library loading ──

    def _load_library(self):
        try:
            self._albums, self._tracks_by_album = self._reader.load()
        except FileNotFoundError as e:
            QMessageBox.critical(self, 'Library Not Found', str(e))
            self._status_bar.showMessage('Library not found.')
            return

        self._populate_album_list(self._albums)
        count = sum(len(v) for v in self._tracks_by_album.values())
        self._status_bar.showMessage(
            f'Ready — {len(self._albums)} albums, {count:,} tracks'
        )
        self._start_artwork_loader(self._albums)

    def _populate_album_list(self, albums: list[AlbumInfo]):
        placeholder = _placeholder_pixmap(60)
        self._album_list.clear()
        self._album_items.clear()

        for album in albums:
            key_str = f'{album.key[0]}|||{album.key[1]}'
            item = QListWidgetItem(QIcon(placeholder), album.album_name)
            item.setData(Qt.ItemDataRole.UserRole, album)
            item.setToolTip(f'{album.display_artist} — {album.track_count} tracks')
            self._album_list.addItem(item)
            self._album_items[key_str] = item

    def _start_artwork_loader(self, albums: list[AlbumInfo]):
        self._artwork_queue: dict[str, str] = {
            f'{a.key[0]}|||{a.key[1]}': a.first_track_path
            for a in albums
        }
        self._artwork_loaded: set[str] = set()
        self._artwork_thread: ArtworkThread | None = None

        self._load_visible_artwork()
        self._album_list.verticalScrollBar().valueChanged.connect(
            lambda _: self._load_visible_artwork()
        )

    def _load_visible_artwork(self):
        """Enqueue artwork for visible album list items not yet loaded."""
        pending: list[tuple[str, str]] = []
        list_rect = self._album_list.rect()
        for i in range(self._album_list.count()):
            item = self._album_list.item(i)
            if item is None or item.isHidden():
                continue
            if not list_rect.intersects(self._album_list.visualItemRect(item)):
                continue
            album: AlbumInfo = item.data(Qt.ItemDataRole.UserRole)
            key_str = f'{album.key[0]}|||{album.key[1]}'
            if key_str not in self._artwork_loaded:
                path = self._artwork_queue.get(key_str)
                if path:
                    pending.append((key_str, path))

        if not pending:
            return

        if self._artwork_thread and self._artwork_thread.isRunning():
            self._artwork_thread.stop()
            self._artwork_thread.wait(100)

        self._artwork_thread = ArtworkThread(pending)
        self._artwork_thread.artwork_ready.connect(self._on_artwork_ready)
        self._artwork_thread.start()

    def _on_artwork_ready(self, key_str: str, image_bytes: bytes):
        self._artwork_loaded.add(key_str)
        pixmap = _pixmap_from_bytes(image_bytes, 60) if image_bytes else _placeholder_pixmap(60)
        item = self._album_items.get(key_str)
        if item:
            item.setIcon(QIcon(pixmap))

    def _filter_albums(self, text: str):
        query = text.lower()
        for i in range(self._album_list.count()):
            item = self._album_list.item(i)
            album: AlbumInfo = item.data(Qt.ItemDataRole.UserRole)
            hidden = (
                query not in album.album_name.lower()
                and query not in album.display_artist.lower()
            )
            item.setHidden(hidden)

    # ── Album selection ──

    def _on_album_selected(self, current, previous):
        if current is None:
            return
        album: AlbumInfo = current.data(Qt.ItemDataRole.UserRole)
        self._current_key = album.key
        tracks = self._tracks_by_album.get(album.key, [])
        self._populate_song_table(tracks)
        self._sort_btn.setEnabled(True)
        self._apply_btn.setEnabled(True)

    def _populate_song_table(self, tracks: list[TrackInfo]):
        self._song_table.setRowCount(0)

        disc_nums = {t.disc_number for t in tracks if t.disc_number > 0}
        show_disc = len(disc_nums) > 1
        self._song_table.setColumnHidden(4, not show_disc)

        for i, track in enumerate(tracks):
            self._song_table.insertRow(i)

            date_str = (
                track.date_added.strftime('%Y-%m-%d')
                if track.date_added else ''
            )

            num_item = QTableWidgetItem(str(track.track_number))
            num_item.setData(Qt.ItemDataRole.UserRole, track)
            num_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )

            self._song_table.setItem(i, 0, num_item)
            self._song_table.setItem(i, 1, QTableWidgetItem(track.title))
            self._song_table.setItem(i, 2, QTableWidgetItem(track.artist))
            self._song_table.setItem(i, 3, QTableWidgetItem(date_str))
            self._song_table.setItem(i, 4, QTableWidgetItem(str(track.disc_number or '')))

    def _replace_saved_track_in_library(self, updated_track: TrackInfo) -> tuple[str, str]:
        new_key = _album_key_for_track(updated_track)
        found_key = None
        found_index = None

        candidate_keys = []
        if self._current_key in self._tracks_by_album:
            candidate_keys.append(self._current_key)
        candidate_keys.extend(
            key for key in self._tracks_by_album
            if key not in candidate_keys
        )

        for key in candidate_keys:
            for i, track in enumerate(self._tracks_by_album[key]):
                if track.file_path == updated_track.file_path:
                    found_key = key
                    found_index = i
                    break
            if found_key is not None:
                break

        if found_key is None or found_index is None:
            self._tracks_by_album.setdefault(new_key, []).append(updated_track)
        elif found_key == new_key:
            self._tracks_by_album[found_key][found_index] = updated_track
        else:
            old_tracks = self._tracks_by_album[found_key]
            del old_tracks[found_index]
            if not old_tracks:
                del self._tracks_by_album[found_key]
            self._tracks_by_album.setdefault(new_key, []).append(updated_track)

        # _rebuild_albums_from_tracks reads _tracks_by_album (already mutated above)
        # and overwrites _albums; _current_key must be assigned AFTER the rebuild so
        # any re-entrant callers see a consistent (_albums, _current_key) pair.
        self._rebuild_albums_from_tracks()
        self._current_key = new_key
        return new_key

    def _rebuild_albums_from_tracks(self):
        albums: list[AlbumInfo] = []
        for key, tracks in list(self._tracks_by_album.items()):
            if not tracks:
                del self._tracks_by_album[key]
                continue

            first_track = tracks[0]
            _, album_name, display_artist = _album_identity(
                first_track.artist,
                first_track.album_artist,
                first_track.album,
                first_track.compilation,
            )
            albums.append(AlbumInfo(
                key=key,
                album_name=album_name,
                display_artist=display_artist,
                first_track_path=first_track.file_path,
                track_count=len(tracks),
            ))

        albums.sort(key=lambda a: a.album_name.lower())
        self._albums = albums

    # ── Sort and track number actions ──

    def _sort_by_date_added(self):
        if self._current_key is None:
            return
        tracks = self._tracks_by_album.get(self._current_key, [])
        sorted_tracks = sorted(
            tracks, key=lambda t: t.date_added or datetime.min
        )
        self._populate_song_table(sorted_tracks)

    def _current_table_tracks(self) -> list[TrackInfo]:
        tracks = []
        for row in range(self._song_table.rowCount()):
            item = self._song_table.item(row, 0)
            if item:
                tracks.append(item.data(Qt.ItemDataRole.UserRole))
        return tracks

    def _apply_track_numbers(self):
        tracks = self._current_table_tracks()
        if not tracks:
            return

        self._status_bar.showMessage(f'Applying track numbers to {len(tracks)} tracks…')
        print(f'[library_manager] Applying track numbers — {len(tracks)} tracks', flush=True)

        by_disc: dict[int, list[tuple[int, TrackInfo]]] = defaultdict(list)
        for row, track in enumerate(tracks):
            disc = track.disc_number or 1
            by_disc[disc].append((row, track))

        drm_count = 0
        write_errors: list[str] = []
        successful: list[tuple[int, int, TrackInfo]] = []  # all correct-numbered rows (UI update)
        file_written: list[str] = []                        # paths actually written to disk (COM sync)

        for disc in sorted(by_disc.keys()):
            for new_num, (row, track) in enumerate(by_disc[disc], start=1):
                orig_num = track.track_number
                try:
                    if orig_num == new_num:
                        successful.append((row, new_num, track))
                        print(f'[library_manager]   --    {new_num:>3}  {Path(track.file_path).name} (unchanged)', flush=True)
                        continue
                    track.track_number = new_num
                    errs = self._writer.write_track_number(track)
                    if errs:
                        write_errors.extend(errs)
                        track.track_number = orig_num  # revert — file not written
                        print(f'[library_manager]   SKIP  {Path(track.file_path).name}: {errs[0]}', flush=True)
                    else:
                        successful.append((row, new_num, track))
                        file_written.append(track.file_path)
                        print(f'[library_manager]   OK    {new_num:>3}  {Path(track.file_path).name}', flush=True)
                except DRMError:
                    track.track_number = orig_num
                    drm_count += 1
                    print(f'[library_manager]   DRM   {Path(track.file_path).name}', flush=True)
                except Exception as e:
                    track.track_number = orig_num
                    write_errors.append(str(e))
                    print(f'[library_manager]   ERR   {Path(track.file_path).name}: {e}', flush=True)

        for row, new_num, _ in successful:
            self._song_table.item(row, 0).setText(str(new_num))

        if file_written:
            print(f'[library_manager] Syncing {len(file_written)} changed tracks to iTunes…', flush=True)
            try:
                com_errors = self._refresher.refresh(file_written)
                write_errors.extend(com_errors)
                for err in com_errors:
                    print(f'[library_manager]   COM   {err}', flush=True)
            except RuntimeError as e:
                write_errors.append(str(e))
                print(f'[library_manager]   COM   {e}', flush=True)

        parts = [f'Track numbers applied to {len(successful)} tracks.']
        if drm_count:
            parts.append(f'{drm_count} DRM files skipped.')
        if write_errors:
            parts.append(f'{len(write_errors)} errors — see console.')
        self._status_bar.showMessage(' '.join(parts))
        print(f'[library_manager] Done — {len(successful)} written, {len(write_errors)} errors, {drm_count} DRM skipped.', flush=True)

    # ── Metadata dialog ──

    def _open_metadata_dialog(self, index):
        row = index.row()
        item = self._song_table.item(row, 0)
        if item is None:
            return
        track: TrackInfo = item.data(Qt.ItemDataRole.UserRole)

        dialog = MetadataDialog(track, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        updated_track, artwork_bytes = dialog.updated_track_and_artwork()

        drm_skipped = False
        write_errors: list[str] = []
        write_ok = False
        try:
            errs = self._writer.write_all(updated_track, artwork_bytes)
            write_errors.extend(errs)
            write_ok = not errs  # any returned error means file was not fully written
        except DRMError:
            drm_skipped = True

        if write_ok:
            try:
                ratings = {updated_track.file_path: updated_track.rating}
                com_errors = self._refresher.refresh([updated_track.file_path], ratings)
                write_errors.extend(com_errors)
            except RuntimeError as e:
                write_errors.append(str(e))

            item.setData(Qt.ItemDataRole.UserRole, updated_track)
            self._song_table.item(row, 0).setText(str(updated_track.track_number))
            self._song_table.item(row, 1).setText(updated_track.title)
            self._song_table.item(row, 2).setText(updated_track.artist)
            self._song_table.item(row, 4).setText(str(updated_track.disc_number or ''))

            old_key = self._current_key
            new_key = self._replace_saved_track_in_library(updated_track)
            if new_key != old_key:
                self._populate_album_list(self._albums)
                self._filter_albums(self._search.text())
                new_key_str = f'{new_key[0]}|||{new_key[1]}'
                new_item = self._album_items.get(new_key_str)
                if new_item is not None:
                    self._album_list.setCurrentItem(new_item)
                else:
                    self._populate_song_table(self._tracks_by_album.get(new_key, []))

        if drm_skipped:
            self._status_bar.showMessage('DRM file — metadata not written.')
        elif not write_ok:
            for err in write_errors:
                print(f'[library_manager] {err}', file=sys.stderr)
            self._status_bar.showMessage(f'Failed to save "{updated_track.title}" — see console.')
        elif write_errors:
            for err in write_errors:
                print(f'[library_manager] {err}', file=sys.stderr)
            self._status_bar.showMessage(f'Saved: {updated_track.title} (iTunes sync failed — see console.)')
        else:
            self._status_bar.showMessage(f'Saved: {updated_track.title}')

    def closeEvent(self, event):
        if getattr(self, '_artwork_thread', None) and self._artwork_thread.isRunning():
            self._artwork_thread.stop()
            self._artwork_thread.wait(500)
        self._refresher.disconnect()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setApplicationName('iTunes Library Manager')
    window = LibraryManagerApp()
    window.show()
    sys.exit(app.exec())
