"""
scripts/benchmark_shuffle.py - Runtime and quality benchmark for shuffle_core

Generates synthetic track objects (no GUI dependencies) and measures:
  - Median and P95 wall-clock time for smart_shuffle
  - Adjacent same-artist clump count (quality metric)

Usage:
    python scripts/benchmark_shuffle.py
    python scripts/benchmark_shuffle.py --sizes 1000 5000 15000 --iters 5
"""

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from shuffle_core import CustomShuffleAlgorithm


# ---------------------------------------------------------------------------
# Synthetic track factory (no MusicTrack / GUI imports)
# ---------------------------------------------------------------------------

class _FakeTrack:
    __slots__ = ('filepath', 'artist', 'album', 'genre',
                 'play_count', 'skips', 'rating', 'loved',
                 'last_played', 'date_added', 'bpm')

    def __init__(self, i, n_artists, n_albums, n_genres):
        now = time.time()
        self.filepath    = f"/synthetic/track_{i:06d}.mp3"
        self.artist      = f"Artist_{i % n_artists}"
        self.album       = f"Album_{i % n_albums}"
        self.genre       = f"Genre_{i % n_genres}"
        self.play_count  = random.randint(0, 50)
        self.skips       = random.randint(0, self.play_count // 2 + 1)
        self.rating      = random.choice([0, 20, 40, 60, 80, 100])
        self.loved       = random.choice([True, False, None])
        self.last_played = now - random.uniform(0, 365 * 86400) if random.random() > 0.2 else None
        self.date_added  = now - random.uniform(0, 3 * 365 * 86400)
        self.bpm         = random.choice([0, 0, 0, 120, 128, 140])


def _make_tracks(n):
    n_artists = max(1, n // 8)
    n_albums  = max(1, n // 20)
    n_genres  = max(1, min(20, n // 50))
    return [_FakeTrack(i, n_artists, n_albums, n_genres) for i in range(n)]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _adjacent_repeats(result, key_fn):
    """Count adjacent positions where key_fn(result[i]) == key_fn(result[i-1])."""
    return sum(
        1 for i in range(1, len(result))
        if key_fn(result[i]) == key_fn(result[i - 1]) and key_fn(result[i])
    )


def _percentile(sorted_vals, pct):
    idx = int(len(sorted_vals) * pct / 100)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_benchmark(sizes, iters, config=None):
    print(f"\n{'='*60}")
    print(f"Shuffle benchmark  |  {iters} iteration(s) per size")
    print(f"{'='*60}")

    for n in sizes:
        print(f"\n--- Library size: {n:,} tracks ---")
        tracks = _make_tracks(n)

        times = []
        artist_repeats = []
        for it in range(iters):
            random.seed(it)
            t0 = time.perf_counter()
            result = CustomShuffleAlgorithm.smart_shuffle(list(tracks), config)
            elapsed = time.perf_counter() - t0
            times.append(elapsed)

            repeats = _adjacent_repeats(result, lambda t: t.artist)
            artist_repeats.append(repeats)

            print(f"  iter {it+1}: {elapsed*1000:.1f} ms  |  same-artist adjacent: {repeats}")

        times.sort()
        print(f"  median: {_percentile(times, 50)*1000:.1f} ms"
              f"  |  p95: {_percentile(times, 95)*1000:.1f} ms"
              f"  |  clumps median: {sorted(artist_repeats)[len(artist_repeats)//2]}")

    print(f"\n{'='*60}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(description="Benchmark shuffle_core.smart_shuffle")
    p.add_argument('--sizes', nargs='+', type=int, default=[1000, 5000, 15000],
                   metavar='N', help='Library sizes to benchmark (default: 1000 5000 15000)')
    p.add_argument('--iters', type=int, default=5,
                   help='Iterations per size (default: 5)')
    return p.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    run_benchmark(args.sizes, args.iters)
