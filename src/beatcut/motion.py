"""Per-clip motion analysis, sampled at 10 Hz.

For every clip this produces:
- motion: mean frame-to-frame change (the plain "how much is happening" score)
- sharp:  the same score, marked down where the frame is blurred or the whole
          picture moves at once (camera whips), so action in a steady shot wins
- cols / rows: where in the frame the change happens, used for motion-aware cropping
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import ffutil

RATE = 10  # samples per second
W, H = 128, 72
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
CACHE_VERSION = 2

CHANGED = 12  # a pixel counts as "changed" above this difference (0-255)


@dataclass
class Clip:
    path: Path
    duration: float
    motion: np.ndarray  # motion[i] = change between t=i/RATE and t=(i+1)/RATE
    sharp: np.ndarray  # motion, discounted for blur and whole-frame movement
    cols: np.ndarray  # (n, W) change per column
    rows: np.ndarray  # (n, H) change per row

    def scores(self, pick: str) -> np.ndarray:
        return self.sharp if pick == "sharp" else self.motion

    def focus(self, start: float, dur: float) -> tuple[float, float]:
        """Centre of the action in a window, as fractions of width and height."""
        a = int(start * RATE)
        b = max(a + 1, int(np.ceil((start + dur) * RATE)))
        return _centroid(self.cols[a:b].sum(axis=0)), _centroid(self.rows[a:b].sum(axis=0))


def _centroid(profile: np.ndarray) -> float:
    if profile.size == 0:
        return 0.5
    weight = np.maximum(profile - np.median(profile), 0.0)
    total = float(weight.sum())
    if total < 1e-6:
        return 0.5
    positions = (np.arange(len(weight)) + 0.5) / len(weight)
    return round(float((weight * positions).sum() / total), 3)


def _empty(path: Path) -> Clip:
    z = np.zeros(0, dtype=np.float32)
    return Clip(path, 0.0, z, z, np.zeros((0, W), np.float32), np.zeros((0, H), np.float32))


def find_clips(folder: Path) -> list[Path]:
    paths = sorted(p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("."))
    if not paths:
        raise ValueError(f"No video clips found in {folder}")
    return paths


def analyse_clip(path: Path) -> Clip:
    raw = ffutil.ffmpeg(
        [*ffutil.HWACCEL, "-i", str(path), "-an", "-vf", f"fps={RATE},scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
        capture=True,
    )
    frames = np.frombuffer(raw, dtype=np.uint8)
    frames = frames[: len(frames) // (W * H) * (W * H)].reshape(-1, H, W)
    if len(frames) < 2:
        return _empty(path)

    n = len(frames) - 1
    motion = np.empty(n, np.float32)
    coverage = np.empty(n, np.float32)
    cols = np.empty((n, W), np.float32)
    rows = np.empty((n, H), np.float32)
    detail = np.empty(n + 1, np.float32)  # edge strength per frame; drops when blurred
    for a in range(0, n + 1, 256):  # in chunks, so long clips stay small in memory
        chunk = frames[a : a + 257].astype(np.int16)
        detail[a : a + len(chunk)] = (
            np.abs(np.diff(chunk, axis=2)).mean(axis=(1, 2)) + np.abs(np.diff(chunk, axis=1)).mean(axis=(1, 2))
        )
        diff = np.abs(np.diff(chunk, axis=0))
        b = a + len(diff)
        motion[a:b] = diff.mean(axis=(1, 2))
        coverage[a:b] = (diff > CHANGED).mean(axis=(1, 2))
        cols[a:b] = diff.mean(axis=1)
        rows[a:b] = diff.mean(axis=2)

    # Sharpness relative to the clip's own crisp frames.
    crisp = float(np.percentile(detail, 90)) or 1.0
    relative = np.clip(np.minimum(detail[:-1], detail[1:]) / crisp, 0.0, 1.0)
    # When more than half the picture changes at once it is the camera moving, not the subject.
    whip = np.clip((coverage - 0.5) / 0.4, 0.0, 1.0)
    sharp = (motion * relative**2 * (1.0 - 0.8 * whip)).astype(np.float32)

    duration = min(ffutil.duration(path), len(frames) / RATE)
    return Clip(path, duration, motion, sharp, cols, rows)


def analyse_clips(paths: list[Path]) -> list[Clip]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(analyse_clip, paths))


def save_clip(clip: Clip, dest: Path) -> None:
    np.savez_compressed(
        dest, version=CACHE_VERSION, duration=clip.duration,
        motion=clip.motion, sharp=clip.sharp, cols=clip.cols, rows=clip.rows,
    )


def load_clip(path: Path, cache: Path) -> Clip | None:
    """Read a cached analysis, or None when it is missing or from an older version."""
    try:
        with np.load(cache) as data:
            if int(data["version"]) != CACHE_VERSION:
                return None
            return Clip(path, float(data["duration"]), data["motion"], data["sharp"], data["cols"], data["rows"])
    except (OSError, KeyError, ValueError):
        return None
