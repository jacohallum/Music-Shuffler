"""
shuffle_core.py - Custom shuffle algorithms (standalone, no GUI dependencies)

Provides CustomShuffleAlgorithm with:
  - smart_shuffle: Draft + Repair (O(n log n)) with adaptive spacing and history guard
  - truly_random: Standard random shuffle

New in v2:
  - adaptive_constraints: auto-scales spacing windows from library cardinality
  - recent_history guard: penalises recently-played tracks/artists in weight phase
  - Precomputed per-track keys: eliminates repeated string normalisation in repair pass
"""

import math
import os
import random
import time
from collections import defaultdict, deque


class CustomShuffleAlgorithm:
    """Custom shuffle algorithms for music playback"""

    @staticmethod
    def smart_shuffle(tracks, config=None, recent_history=None):
        """
        Smart Shuffle v2 - Optimised for large libraries (15K+ tracks)

        Uses Draft + Repair algorithm:
          Phase 1: Weighted sort (O(n log n))
          Phase 2: Fix clumping (O(n))

        Parameters
        ----------
        tracks : list
            MusicTrack objects (or any objects with the expected attributes).
        config : dict, optional
            Shuffle configuration; falls back to hard-coded defaults for every key.
        recent_history : list of (norm_path, artist_key_or_None), optional
            Recently-played entries used by the history guard.  Each element is a
            2-tuple ``(normalized_filepath, artist_key_string_or_None)``.
        """
        if not tracks:
            return []

        if config is None:
            config = {}

        # ---- Constraint windows ------------------------------------------------
        RECENT_ARTISTS = config.get('recent_artists', 3)
        RECENT_ALBUMS  = config.get('recent_albums',  2)
        RECENT_GENRES  = config.get('recent_genres',  2)
        LOOKAHEAD      = config.get('lookahead', 400)

        # Unknown value sentinels (don't constrain missing tags)
        UNKNOWN_ARTIST = {"", "unknown artist"}
        UNKNOWN_ALBUM  = {"", "unknown album"}
        UNKNOWN_GENRE  = {"", "unknown"}

        now = time.time()

        # ---- Helper / scoring functions ----------------------------------------
        def _norm_rating(r):
            if r is None:
                return 0.50
            try:
                r = int(r or 0)
            except Exception:
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
            except Exception:
                return 0.60
            pc_eff = max(pc, 0) + 1
            return 1.0 / (1.0 + pc_eff)

        def _recency_boost(last_played):
            if last_played is None:
                return 1.0
            try:
                lp = float(last_played)
            except Exception:
                return 1.0
            if lp <= 0:
                return 1.0
            age_days = max(0.0, (now - lp) / 86400.0)
            return 1.0 - math.exp(-age_days / 14.0)

        def _newness_boost(date_added):
            if date_added is None:
                return 0.20
            try:
                da = float(date_added)
            except Exception:
                return 0.20
            if da <= 0:
                return 0.20
            age_days = max(0.0, (now - da) / 86400.0)
            return 1.0 / (1.0 + (age_days / 30.0))

        def _skip_penalty(t):
            pc = getattr(t, 'play_count', 0) or 0
            sk = getattr(t, 'skips', 0) or 0
            if sk == 0:
                return 0.0
            return 0.4 * math.tanh(sk / (pc + 1.0))

        def _love_score(loved):
            if loved is None:
                return 0.25
            return 1.0 if loved else 0.0

        # ---- Precompute per-track keys (Task 7: avoids repeated normalisation) --
        # Keys are stored as (artist_key, album_key, genre_key) indexed by id(t).
        _pk = {}
        for t in tracks:
            a   = (t.artist or "").strip()
            ak  = a.lower() if a and a.lower() not in UNKNOWN_ARTIST else None

            alb  = (t.album or "").strip()
            albl = alb.lower() if alb and alb.lower() not in UNKNOWN_ALBUM else None
            albk = f"{ak}||{albl}" if albl is not None else None

            g   = (getattr(t, 'genre', '') or '').strip()
            gk  = g.lower() if g and g.lower() not in UNKNOWN_GENRE else None

            _pk[id(t)] = (ak, albk, gk)

        def artist_key(t): return _pk[id(t)][0]
        def album_key(t):  return _pk[id(t)][1]
        def genre_key(t):  return _pk[id(t)][2]

        # ---- Adaptive constraint windows (Task 4) ------------------------------
        if config.get('adaptive_constraints', True):
            unique_artists = len({v[0] for v in _pk.values() if v[0] is not None})
            unique_albums  = len({v[1] for v in _pk.values() if v[1] is not None})
            unique_genres  = len({v[2] for v in _pk.values() if v[2] is not None})

            adaptive_artists = min(8, max(3, round(math.sqrt(unique_artists) / 2)))
            adaptive_albums  = min(6, max(2, round(math.sqrt(unique_albums)  / 3)))
            adaptive_genres  = min(4, max(1, round(math.sqrt(unique_genres)  / 4)))

            RECENT_ARTISTS = max(RECENT_ARTISTS, adaptive_artists)
            RECENT_ALBUMS  = max(RECENT_ALBUMS,  adaptive_albums)
            RECENT_GENRES  = max(RECENT_GENRES,  adaptive_genres)

        # ---- History guard sets (Task 5) ----------------------------------------
        recent_track_set  = set()
        recent_artist_set = set()
        if recent_history:
            guard_size = config.get('history_guard_size', 30)
            recent_n = recent_history[-guard_size:] if len(recent_history) > guard_size else recent_history
            # Normalise stored paths so comparisons work cross-platform (Windows
            # normcase converts forward slashes → backslashes and lowercases).
            recent_track_set  = {os.path.normcase(os.path.normpath(fp)) for fp, _ in recent_n}
            recent_artist_set = {ak for _, ak in recent_n if ak is not None}

        # ---- Artist-level skip rate aggregation --------------------------------
        artist_plays = defaultdict(int)
        artist_skips = defaultdict(int)
        for t in tracks:
            ak = artist_key(t)
            if ak is None:
                continue
            artist_plays[ak] += getattr(t, 'play_count', 0) or 0
            artist_skips[ak] += getattr(t, 'skips', 0) or 0

        artist_skip_rate = {
            ak: math.tanh((artist_skips.get(ak, 0)) / (plays + 1.0))
            for ak, plays in artist_plays.items()
        }

        # ---- PHASE 1: DRAFT (compute weight once per track, then sort) ----------
        track_penalty_w  = config.get('history_track_penalty',  0.35)
        artist_penalty_w = config.get('history_artist_penalty', 0.15)

        def _compute_weight(t):
            random_min   = config.get('random_min',   0.20)
            random_range = config.get('random_range', 0.50)
            w = random_min + random.random() * random_range

            w += config.get('rating_weight',       0.55) * _norm_rating(getattr(t, 'rating', None))
            w += config.get('novelty_weight',      0.55) * _novelty_score(getattr(t, 'play_count', None))
            w += config.get('recency_weight',      0.45) * _recency_boost(getattr(t, 'last_played', None))
            w += config.get('newness_weight',      0.20) * _newness_boost(getattr(t, 'date_added', None))
            w += config.get('loved_weight',        0.30) * _love_score(getattr(t, 'loved', None))
            w -= config.get('skip_penalty_weight', 0.40) * _skip_penalty(t)

            ak = artist_key(t)
            if ak is not None:
                w -= config.get('artist_skip_weight', 0.10) * artist_skip_rate.get(ak, 0.0)

            if (getattr(t, 'bpm', 0) or 0) > 0:
                w += config.get('bpm_bonus', 0.05)

            # History guard penalties (Task 5)
            if recent_track_set:
                norm_fp = os.path.normcase(os.path.normpath(getattr(t, 'filepath', '') or ''))
                if norm_fp in recent_track_set:
                    w -= track_penalty_w
                if ak is not None and ak in recent_artist_set:
                    w -= artist_penalty_w

            return max(w, 0.001)

        # Weighted sort  O(n log n)
        weighted_tracks = [(-_compute_weight(t), t) for t in tracks]
        weighted_tracks.sort(key=lambda x: x[0])
        draft = [t for _, t in weighted_tracks]

        # ---- PHASE 2: REPAIR (fix clumping by swapping) -------------------------
        result         = []
        recent_artists = deque(maxlen=RECENT_ARTISTS)
        recent_albums  = deque(maxlen=RECENT_ALBUMS)
        recent_genres  = deque(maxlen=RECENT_GENRES)

        i = 0
        while i < len(draft):
            track = draft[i]

            def passes_constraints(t, relax_level):
                """
                0: all constraints enforced
                1: relax genre
                2: relax album
                3: accept anything
                """
                if relax_level >= 3:
                    return True
                ak  = artist_key(t)
                al  = album_key(t)
                gk  = genre_key(t)
                if ak is not None and ak in recent_artists:
                    return False
                if relax_level < 2 and al is not None and al in recent_albums:
                    return False
                if relax_level < 1 and gk is not None and gk in recent_genres:
                    return False
                return True

            placed = False
            for relax_level in range(4):
                if passes_constraints(track, relax_level):
                    result.append(track)
                    ak = artist_key(track)
                    al = album_key(track)
                    gk = genre_key(track)
                    if ak is not None:
                        recent_artists.append(ak)
                    if al is not None:
                        recent_albums.append(al)
                    if gk is not None:
                        recent_genres.append(gk)
                    i += 1
                    placed = True
                    break

                swap_found = False
                for j in range(i + 1, min(len(draft), i + 1 + LOOKAHEAD)):
                    if passes_constraints(draft[j], relax_level):
                        draft[i], draft[j] = draft[j], draft[i]
                        track = draft[i]
                        swap_found = True
                        break

                if swap_found:
                    continue

            if not placed:
                result.append(track)
                ak = artist_key(track)
                al = album_key(track)
                gk = genre_key(track)
                if ak is not None:
                    recent_artists.append(ak)
                if al is not None:
                    recent_albums.append(al)
                if gk is not None:
                    recent_genres.append(gk)
                i += 1

        return result

    @staticmethod
    def truly_random(tracks):
        """Standard random shuffle"""
        shuffled = list(tracks)
        random.shuffle(shuffled)
        return shuffled
