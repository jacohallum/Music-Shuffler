"""
tests/test_library_manager.py — unit tests for non-GUI classes in library_manager.py

Run with:
    python -m unittest tests/test_library_manager.py -v
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utlities.library_manager import (
    TrackInfo, AlbumInfo, DRMError,
    iTunesXMLReader, MetadataWriter, iTunesCOMRefresher,
)


def _make_plist_library():
    """Minimal iTunes plist with 3 tracks: 2 on one album (same album artist),
    1 on a same-named album by a different artist."""
    return {
        'Tracks': {
            '1': {
                'Name': 'Song A',
                'Artist': 'Artist X',
                'Album Artist': 'Album Artist X',
                'Album': 'Album One',
                'Year': 2020,
                'Genre': 'Rock',
                'Composer': 'Composer A',
                'Comments': 'Note A',
                'Track Number': 2,
                'Disc Number': 1,
                'BPM': 120,
                'Rating': 80,
                'Date Added': datetime(2021, 6, 1, 10, 0, 0),
                'Location': 'file:///C:/Music/song_a.mp3',
            },
            '2': {
                'Name': 'Song B',
                'Artist': 'Artist X',
                'Album Artist': 'Album Artist X',
                'Album': 'Album One',
                'Year': 2020,
                'Genre': 'Rock',
                'Track Number': 1,
                'Disc Number': 1,
                'Date Added': datetime(2021, 3, 14, 12, 0, 0),
                'Location': 'file:///C:/Music/song_b.mp3',
            },
            '3': {
                'Name': 'Song C',
                'Artist': 'Artist Y',
                # No Album Artist — should group by Artist
                'Album': 'Album One',
                'Date Added': datetime(2022, 1, 5, 8, 0, 0),
                'Location': 'file:///C:/Music/song_c.mp3',
            },
        }
    }


class TestITunesXMLReader(unittest.TestCase):

    def _reader_with_mock_library(self, library_dict, xml_path='fake/iTunes Library.xml'):
        reader = iTunesXMLReader()
        with patch.object(reader, 'find_xml', return_value=Path(xml_path)), \
             patch('builtins.open', mock_open()), \
             patch('plistlib.load', return_value=library_dict):
            return reader.load()

    def test_raises_when_no_xml_found(self):
        reader = iTunesXMLReader()
        with patch.object(Path, 'exists', return_value=False):
            with self.assertRaises(FileNotFoundError) as ctx:
                reader.find_xml()
        self.assertIn('iTunes Library.xml', str(ctx.exception))
        self.assertIn('iTunes Music Library.xml', str(ctx.exception))

    def test_prefers_first_xml_filename(self):
        reader = iTunesXMLReader()
        with patch.object(Path, 'exists', lambda p: 'iTunes Library.xml' == p.name):
            found = reader.find_xml()
        self.assertEqual(found.name, 'iTunes Library.xml')

    def test_same_album_name_different_artists_creates_two_albums(self):
        albums, tracks_by_album = self._reader_with_mock_library(_make_plist_library())
        self.assertEqual(len(albums), 2)
        keys = {a.key for a in albums}
        self.assertIn(('album artist x', 'album one'), keys)
        self.assertIn(('artist y', 'album one'), keys)

    def test_falls_back_to_artist_when_no_album_artist(self):
        albums, _ = self._reader_with_mock_library(_make_plist_library())
        keys = {a.key for a in albums}
        self.assertIn(('artist y', 'album one'), keys)

    def test_tracks_sorted_by_date_added(self):
        _, tracks_by_album = self._reader_with_mock_library(_make_plist_library())
        key = ('album artist x', 'album one')
        tracks = tracks_by_album[key]
        self.assertEqual(len(tracks), 2)
        # Song B added 2021-03-14 should be first
        self.assertEqual(tracks[0].title, 'Song B')
        self.assertEqual(tracks[1].title, 'Song A')

    def test_date_added_parsed_as_datetime(self):
        _, tracks_by_album = self._reader_with_mock_library(_make_plist_library())
        key = ('album artist x', 'album one')
        track = tracks_by_album[key][0]
        self.assertIsInstance(track.date_added, datetime)

    def test_track_with_no_date_added_gets_none(self):
        lib = _make_plist_library()
        del lib['Tracks']['1']['Date Added']
        del lib['Tracks']['2']['Date Added']
        _, tracks_by_album = self._reader_with_mock_library(lib)
        key = ('album artist x', 'album one')
        for t in tracks_by_album[key]:
            self.assertIsNone(t.date_added)

    def test_file_path_normalized(self):
        _, tracks_by_album = self._reader_with_mock_library(_make_plist_library())
        key = ('album artist x', 'album one')
        for t in tracks_by_album[key]:
            self.assertNotIn('/', t.file_path)
            self.assertTrue(t.file_path[1] == ':')  # Windows drive letter


class TestMetadataWriter(unittest.TestCase):

    def _make_track(self, ext='.mp3', **kwargs):
        defaults = dict(
            title='Test Song', artist='Test Artist', album='Test Album',
            album_artist='Test AA', year='2020', genre='Rock',
            composer='Test Composer', comment='A comment',
            track_number=3, disc_number=1, bpm=128, rating=80,
            date_added=None, file_path=f'C:\\Music\\song{ext}',
        )
        defaults.update(kwargs)
        return TrackInfo(**defaults)

    def test_drm_file_raises_drm_error(self):
        writer = MetadataWriter()
        track = self._make_track(ext='.m4p')
        with self.assertRaises(DRMError):
            writer.write_all(track)

    @patch('utlities.library_manager.MP3')
    @patch('utlities.library_manager.MutagenFile')
    def test_mp3_writes_id3_tags(self, mock_mutagen_file, mock_mp3_cls):
        mock_audio = MagicMock()
        mock_audio.tags = {}
        mock_mp3_cls.return_value = mock_audio

        writer = MetadataWriter()
        track = self._make_track(ext='.mp3')
        writer.write_all(track)

        mock_audio.save.assert_called_once()

    @patch('utlities.library_manager.MP4')
    def test_mp4_writes_atoms(self, mock_mp4_cls):
        mock_audio = MagicMock()
        mock_mp4_cls.return_value = mock_audio

        writer = MetadataWriter()
        track = self._make_track(ext='.m4a')
        writer.write_all(track)

        mock_audio.save.assert_called_once()

    def test_aac_does_not_raise_on_write_error(self):
        writer = MetadataWriter()
        track = self._make_track(ext='.aac')
        with patch('utlities.library_manager.MutagenFile', side_effect=Exception("bad file")):
            skipped = writer.write_all(track)
        self.assertTrue(len(skipped) > 0)

    def test_rating_not_written_to_file(self):
        """Rating is COM-only. write_all must not attempt to write it to tags."""
        writer = MetadataWriter()
        track = self._make_track(ext='.mp3', rating=80)
        with patch('utlities.library_manager.MP3') as mock_mp3_cls:
            mock_audio = MagicMock()
            mock_audio.tags = {}
            mock_mp3_cls.return_value = mock_audio
            writer.write_all(track)
            for call in mock_audio.__setitem__.call_args_list:
                self.assertNotIn('POPM', str(call))

    def test_write_track_number_drm_raises(self):
        writer = MetadataWriter()
        track = self._make_track(ext='.m4p')
        with self.assertRaises(DRMError):
            writer.write_track_number(track)

    @patch('utlities.library_manager.MP3')
    def test_write_track_number_only(self, mock_mp3_cls):
        mock_audio = MagicMock()
        mock_tags = MagicMock()
        mock_audio.tags = mock_tags
        mock_mp3_cls.return_value = mock_audio

        writer = MetadataWriter()
        track = self._make_track(ext='.mp3', track_number=5)
        writer.write_track_number(track)

        mock_audio.save.assert_called_once()
        written_keys = [call.args[0] for call in mock_tags.__setitem__.call_args_list]
        self.assertEqual(written_keys, ['TRCK'])

    @patch('utlities.library_manager.MP4')
    def test_write_track_number_m4a_preserves_total(self, mock_mp4_cls):
        mock_audio = MagicMock()
        mock_audio.get.return_value = [(3, 12)]
        mock_mp4_cls.return_value = mock_audio

        writer = MetadataWriter()
        track = self._make_track(ext='.m4a', track_number=7)
        writer.write_track_number(track)

        mock_audio.__setitem__.assert_called_once_with('trkn', [(7, 12)])
        mock_audio.save.assert_called_once()


class TestITunesCOMRefresher(unittest.TestCase):

    def _make_mock_itunes(self, track_locations: list[str]):
        """Return a mock iTunes COM object whose LibraryPlaylist.Tracks contains
        one COM track per location string."""
        mock_tracks = []
        for loc in track_locations:
            t = MagicMock()
            t.Location = loc
            mock_tracks.append(t)

        mock_playlist = MagicMock()
        mock_playlist.Tracks.Count = len(mock_tracks)
        mock_playlist.Tracks.Item.side_effect = lambda i: mock_tracks[i - 1]

        mock_itunes = MagicMock()
        mock_itunes.LibraryPlaylist = mock_playlist
        return mock_itunes, mock_tracks

    @patch('utlities.library_manager.win32com.client.GetActiveObject')
    def test_connect_tries_get_active_object_first(self, mock_gao):
        mock_gao.return_value = MagicMock()
        mock_gao.return_value.LibraryPlaylist.Tracks.Count = 0
        refresher = iTunesCOMRefresher()
        refresher.connect()
        mock_gao.assert_called_once_with('iTunes.Application')

    @patch('utlities.library_manager.win32com.client.Dispatch')
    @patch('utlities.library_manager.win32com.client.GetActiveObject')
    def test_connect_falls_back_to_dispatch_when_gao_fails(self, mock_gao, mock_dispatch):
        import pywintypes
        mock_gao.side_effect = pywintypes.com_error(-2147221005, 'ClassNotRegistered', None, None)
        mock_dispatch.return_value = MagicMock()
        mock_dispatch.return_value.LibraryPlaylist.Tracks.Count = 0
        refresher = iTunesCOMRefresher()
        refresher.connect()
        mock_dispatch.assert_called_once_with('iTunes.Application')

    @patch('utlities.library_manager.win32com.client.Dispatch')
    @patch('utlities.library_manager.win32com.client.GetActiveObject')
    def test_connect_raises_runtime_error_when_both_fail(self, mock_gao, mock_dispatch):
        import pywintypes
        err = pywintypes.com_error(-2147221005, 'ClassNotRegistered', None, None)
        mock_gao.side_effect = err
        mock_dispatch.side_effect = err
        refresher = iTunesCOMRefresher()
        with self.assertRaises(RuntimeError) as ctx:
            refresher.connect()
        self.assertIn('iTunes', str(ctx.exception))

    @patch('utlities.library_manager.win32com.client.GetActiveObject')
    def test_path_cache_built_on_connect(self, mock_gao):
        locations = [r'C:\Music\song_a.mp3', r'C:\Music\song_b.mp3']
        mock_itunes, _ = self._make_mock_itunes(locations)
        mock_gao.return_value = mock_itunes

        refresher = iTunesCOMRefresher()
        refresher.connect()

        norm_a = os.path.normcase(os.path.normpath(locations[0]))
        norm_b = os.path.normcase(os.path.normpath(locations[1]))
        self.assertIn(norm_a, refresher._path_cache)
        self.assertIn(norm_b, refresher._path_cache)

    @patch('utlities.library_manager.win32com.client.GetActiveObject')
    def test_refresh_calls_update_info_from_file(self, mock_gao):
        loc = r'C:\Music\song_a.mp3'
        mock_itunes, mock_tracks = self._make_mock_itunes([loc])
        mock_gao.return_value = mock_itunes

        refresher = iTunesCOMRefresher()
        refresher.connect()
        refresher.refresh([loc])

        mock_tracks[0].UpdateInfoFromFile.assert_called_once()

    @patch('utlities.library_manager.win32com.client.GetActiveObject')
    def test_refresh_sets_rating_via_com(self, mock_gao):
        loc = r'C:\Music\song_a.mp3'
        mock_itunes, mock_tracks = self._make_mock_itunes([loc])
        mock_gao.return_value = mock_itunes

        refresher = iTunesCOMRefresher()
        refresher.connect()
        refresher.refresh([loc], ratings={loc: 80})

        self.assertEqual(mock_tracks[0].Rating, 80)

    @patch('utlities.library_manager.win32com.client.GetActiveObject')
    def test_refresh_returns_error_for_unknown_path(self, mock_gao):
        mock_itunes, _ = self._make_mock_itunes([r'C:\Music\other.mp3'])
        mock_gao.return_value = mock_itunes

        refresher = iTunesCOMRefresher()
        refresher.connect()
        errors = refresher.refresh([r'C:\Music\missing.mp3'])

        self.assertEqual(len(errors), 1)
        self.assertIn('missing.mp3', errors[0])


if __name__ == '__main__':
    unittest.main()
