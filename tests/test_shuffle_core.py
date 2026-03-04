"""
tests/test_shuffle_core.py - stdlib-only unit tests for shuffle_core.py

Run with:
    python -m unittest tests/test_shuffle_core.py -v
"""

import os
import random
import sys
import unittest

# Allow running from project root or tests/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from shuffle_core import CustomShuffleAlgorithm


class FakeTrack:
    """Minimal track-like object for testing (no GUI dependencies)."""

    def __init__(self, filepath, artist='', album='', genre='',
                 play_count=0, skips=0, rating=0, loved=None,
                 last_played=None, date_added=None, bpm=0):
        self.filepath    = filepath
        self.artist      = artist
        self.album       = album
        self.genre       = genre
        self.play_count  = play_count
        self.skips       = skips
        self.rating      = rating
        self.loved       = loved
        self.last_played = last_played
        self.date_added  = date_added
        self.bpm         = bpm


def _make_tracks(n, prefix='track', artist_pool=None, genre_pool=None):
    """Generate *n* FakeTrack objects with varied metadata."""
    tracks = []
    for i in range(n):
        artist = (artist_pool[i % len(artist_pool)]
                  if artist_pool else f"Artist_{i % max(1, n // 5)}")
        genre  = (genre_pool[i % len(genre_pool)]
                  if genre_pool else f"Genre_{i % 4}")
        tracks.append(FakeTrack(
            filepath   = f"/music/{prefix}_{i:04d}.mp3",
            artist     = artist,
            album      = f"Album_{i % max(1, n // 10)}",
            genre      = genre,
            play_count = i % 20,
            skips      = i % 3,
            rating     = (i % 5) + 1,
        ))
    return tracks


class TestSmartShuffleCompleteness(unittest.TestCase):
    """smart_shuffle must return every input track exactly once."""

    def _assert_permutation(self, tracks, result):
        self.assertEqual(len(result), len(tracks),
                         "Output length differs from input length")
        self.assertEqual(set(id(t) for t in result),
                         set(id(t) for t in tracks),
                         "Output is not a permutation of the input")

    def test_small_library(self):
        tracks = _make_tracks(10)
        result = CustomShuffleAlgorithm.smart_shuffle(tracks)
        self._assert_permutation(tracks, result)

    def test_medium_library(self):
        tracks = _make_tracks(500)
        result = CustomShuffleAlgorithm.smart_shuffle(tracks)
        self._assert_permutation(tracks, result)

    def test_single_track(self):
        tracks = _make_tracks(1)
        result = CustomShuffleAlgorithm.smart_shuffle(tracks)
        self._assert_permutation(tracks, result)

    def test_empty_library(self):
        result = CustomShuffleAlgorithm.smart_shuffle([])
        self.assertEqual(result, [])


class TestSmartShuffleRobustness(unittest.TestCase):
    """smart_shuffle must not crash on missing / None / empty metadata."""

    def test_none_metadata(self):
        tracks = [FakeTrack(
            filepath   = f"/music/t{i}.mp3",
            artist     = None,
            album      = None,
            genre      = None,
            play_count = None,
            skips      = None,
            rating     = None,
            loved      = None,
            last_played = None,
            date_added  = None,
            bpm         = None,
        ) for i in range(20)]
        result = CustomShuffleAlgorithm.smart_shuffle(tracks)
        self.assertEqual(len(result), 20)

    def test_empty_strings(self):
        tracks = [FakeTrack(
            filepath = f"/music/t{i}.mp3",
            artist='', album='', genre='',
        ) for i in range(20)]
        result = CustomShuffleAlgorithm.smart_shuffle(tracks)
        self.assertEqual(len(result), 20)

    def test_zero_numeric_fields(self):
        tracks = [FakeTrack(
            filepath   = f"/music/t{i}.mp3",
            play_count = 0,
            skips      = 0,
            rating     = 0,
            bpm        = 0,
        ) for i in range(20)]
        result = CustomShuffleAlgorithm.smart_shuffle(tracks)
        self.assertEqual(len(result), 20)


class TestAdaptiveConstraints(unittest.TestCase):
    """Adaptive constraints should increase spacing for large artist pools."""

    def _count_adjacent_artist_repeats(self, result):
        repeats = 0
        for i in range(1, len(result)):
            if result[i].artist and result[i].artist == result[i - 1].artist:
                repeats += 1
        return repeats

    def test_adaptive_window_exceeds_configured_minimum(self):
        """Adaptive mode must compute spacing from unique cardinality, not track count.

        With 400 tracks across 100 unique artists (sqrt(100)/2 = 5.0 → rounds to 5)
        and a configured minimum of 2, the effective RECENT_ARTISTS must be >= 5.
        We verify this by checking that same-artist adjacency is lower than it would
        be if the minimum (2) were used literally.
        """
        random.seed(42)
        # 400 tracks, 100 unique artists → adaptive_artists = min(8, max(3, round(sqrt(100)/2))) = max(3,5) = 5
        artists = [f"Artist_{i}" for i in range(100)]
        tracks  = _make_tracks(400, artist_pool=artists)

        # Adaptive should compute window = 5 (from cardinality), overriding configured min of 2
        config_adaptive = {'adaptive_constraints': True,  'recent_artists': 2, 'recent_albums': 1, 'recent_genres': 1}
        # Manually set window to 2 to simulate the broken "count not unique" behaviour
        config_min2     = {'adaptive_constraints': False, 'recent_artists': 2, 'recent_albums': 1, 'recent_genres': 1}
        # Ground-truth with window = 5 (what adaptive should produce)
        config_win5     = {'adaptive_constraints': False, 'recent_artists': 5, 'recent_albums': 1, 'recent_genres': 1}

        random.seed(42)
        result_adaptive = CustomShuffleAlgorithm.smart_shuffle(list(tracks), config_adaptive)
        repeats_adaptive = self._count_adjacent_artist_repeats(result_adaptive)

        random.seed(42)
        result_min2 = CustomShuffleAlgorithm.smart_shuffle(list(tracks), config_min2)
        repeats_min2 = self._count_adjacent_artist_repeats(result_min2)

        random.seed(42)
        result_win5 = CustomShuffleAlgorithm.smart_shuffle(list(tracks), config_win5)
        repeats_win5 = self._count_adjacent_artist_repeats(result_win5)

        # Adaptive must produce same or fewer repeats than window=2 baseline
        self.assertLessEqual(repeats_adaptive, repeats_min2,
                             "Adaptive constraints should not produce more repeats than window-2 baseline")

        # Adaptive result must match window=5 result (verifies unique-cardinality path)
        # (same seed → same random weights → same draft → identical repair outcome)
        adaptive_fps = [t.filepath for t in result_adaptive]
        win5_fps     = [t.filepath for t in result_win5]
        self.assertEqual(adaptive_fps, win5_fps,
                         "Adaptive with 100 unique artists should produce identical output to explicit window=5")

    def test_adaptive_reduces_clumping_vs_minimal_constraints(self):
        """With many artists, adaptive mode should reduce same-artist adjacency."""
        random.seed(42)
        artists = [f"Artist_{i}" for i in range(50)]
        tracks  = _make_tracks(200, artist_pool=artists)

        config_adaptive = {'adaptive_constraints': True,  'recent_artists': 2}
        config_off      = {'adaptive_constraints': False, 'recent_artists': 1, 'recent_albums': 1, 'recent_genres': 1}

        random.seed(42)
        result_adaptive = CustomShuffleAlgorithm.smart_shuffle(list(tracks), config_adaptive)
        repeats_adaptive = self._count_adjacent_artist_repeats(result_adaptive)

        random.seed(42)
        result_off = CustomShuffleAlgorithm.smart_shuffle(list(tracks), config_off)
        repeats_off = self._count_adjacent_artist_repeats(result_off)

        self.assertLessEqual(repeats_adaptive, repeats_off,
                             "Adaptive constraints should not produce more repeats than minimal constraints")

    def test_adaptive_enabled_by_default(self):
        """Calling without config should use adaptive_constraints=True."""
        tracks = _make_tracks(100)
        result = CustomShuffleAlgorithm.smart_shuffle(tracks)
        self.assertEqual(len(result), 100)


class TestHistoryGuard(unittest.TestCase):
    """History guard should lower early-rank frequency of recent tracks/artists."""

    def _rank_of_recent(self, result, recent_fps):
        """Return mean rank (0-indexed) of tracks whose filepath is in recent_fps."""
        n = len(result)
        ranks = [i for i, t in enumerate(result) if t.filepath in recent_fps]
        if not ranks:
            return n  # Not found - best case (pushed to end)
        return sum(ranks) / len(ranks)

    def test_history_guard_depresses_recent_tracks(self):
        """Tracks in recent history should appear later on average.

        Design: each track has a unique artist so the repair phase performs no
        swaps.  With random_range=0 the output is a pure weight-ordered list,
        making the penalty's effect fully deterministic.
        """
        n = 100
        # One unique artist per track → repair phase does no swaps at all
        tracks = [FakeTrack(
            filepath   = f"/music/t{i:04d}.mp3",
            artist     = f"UniqueArtist_{i}",
            album      = f"UniqueAlbum_{i}",
            genre      = f"UniqueGenre_{i % 5}",
            play_count = i % 10,   # varied novelty
        ) for i in range(n)]

        # Mark first 10 tracks as recently played
        recent_fps     = {t.filepath for t in tracks[:10]}
        recent_history = [(t.filepath, t.artist.lower()) for t in tracks[:10]]

        # Zero randomness so weights are fully deterministic
        config = {
            'history_guard_size':     10,
            'history_track_penalty':  0.9,  # strong penalty
            'history_artist_penalty': 0.0,
            'random_min':   0.0,
            'random_range': 0.0,
            'adaptive_constraints': False,
            'recent_artists': 1,
            'recent_albums':  1,
            'recent_genres':  1,
        }

        result_with    = CustomShuffleAlgorithm.smart_shuffle(list(tracks), config, recent_history=recent_history)
        result_without = CustomShuffleAlgorithm.smart_shuffle(list(tracks), config, recent_history=None)

        mean_rank_with    = self._rank_of_recent(result_with,    recent_fps)
        mean_rank_without = self._rank_of_recent(result_without, recent_fps)

        self.assertGreater(mean_rank_with, mean_rank_without,
                           "Recent tracks should rank later when history guard is active")

    def test_no_crash_with_empty_history(self):
        tracks = _make_tracks(50)
        result = CustomShuffleAlgorithm.smart_shuffle(tracks, recent_history=[])
        self.assertEqual(len(result), 50)


class TestDeterminism(unittest.TestCase):
    """Output must be deterministic under a fixed random seed."""

    def test_same_seed_same_result(self):
        tracks = _make_tracks(100)
        config = {}

        random.seed(7)
        result_a = CustomShuffleAlgorithm.smart_shuffle(list(tracks), config)

        random.seed(7)
        result_b = CustomShuffleAlgorithm.smart_shuffle(list(tracks), config)

        fps_a = [t.filepath for t in result_a]
        fps_b = [t.filepath for t in result_b]
        self.assertEqual(fps_a, fps_b,
                         "Same seed must produce identical shuffle order")


class TestTrulyRandom(unittest.TestCase):
    def test_permutation(self):
        tracks = _make_tracks(50)
        result = CustomShuffleAlgorithm.truly_random(tracks)
        self.assertEqual(len(result), 50)
        self.assertEqual(set(id(t) for t in result), set(id(t) for t in tracks))

    def test_does_not_mutate_input(self):
        tracks = _make_tracks(20)
        original_order = [t.filepath for t in tracks]
        CustomShuffleAlgorithm.truly_random(tracks)
        self.assertEqual([t.filepath for t in tracks], original_order)


if __name__ == '__main__':
    unittest.main()
