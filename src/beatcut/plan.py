"""Edit planning: where the cuts go, and which part of which clip fills each slot.

Everything here is pure (no ffmpeg), so the same inputs and seed give the same edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .audio import Analysis, best_start
from .motion import RATE, Clip

MIN_GAP = 0.25  # never cut faster than this, in seconds
USAGE_PENALTY = 0.6  # how strongly to spread picks across clips
DOWNBEAT_BONUS = 1.15  # slots that open on a downbeat count as a little stronger

PICKS = ("motion", "sharp")
CROPS = ("centre", "motion")


class NotEnoughFootage(ValueError):
    pass


@dataclass
class Options:
    """Choices about which footage fills the slots."""

    seed: int = 0
    pick: str = "motion"  # "motion": most change; "sharp": change in a steady, unblurred shot
    match_energy: bool = False  # strongest footage on the loudest slots, calm footage on quiet ones
    crop: str = "centre"  # "centre", or "motion" to frame the crop on the action
    trim_edges: float = 0.0  # seconds to avoid at the start and end of every clip

    def to_json(self) -> dict:
        return {"seed": self.seed, "pick": self.pick, "match_energy": self.match_energy,
                "crop": self.crop, "trim_edges": self.trim_edges}


@dataclass
class Lock:
    """A slot the user has pinned to a specific piece of footage."""

    start: float  # slot start in the output, seconds
    clip: str  # clip file name
    clip_start: float


@dataclass
class Segment:
    start: float  # position in the output, seconds
    end: float
    clip: Path
    clip_start: float  # position in the source clip, seconds
    motion: float
    crop_x: float = 0.5  # centre of the crop, as a fraction of the frame
    crop_y: float = 0.5
    locked: bool = False

    def to_json(self) -> dict:
        return {
            "start": round(self.start, 4),
            "end": round(self.end, 4),
            "clip": self.clip.name,
            "clip_start": round(self.clip_start, 3),
            "motion": round(self.motion, 3),
            "crop_x": self.crop_x,
            "crop_y": self.crop_y,
            "locked": self.locked,
        }


# ---------- timing ----------


def window(analysis: Analysis, start: float | str, wanted: float, fps: int) -> tuple[float, float]:
    """Resolve where in the track the edit sits: (start, duration), clamped to the track."""
    if start == "auto":
        start = best_start(analysis, min(wanted, analysis.duration))
    start = max(0.0, min(float(start), analysis.duration - 1.0))
    duration = max(1.0, min(float(wanted), analysis.duration - start))
    return round(start, 3), int(duration * fps + 1e-6) / fps


def cut_times(analysis: Analysis, every: int, duration: float, fps: int, start: float = 0.0) -> list[float]:
    """Cut points on the beat grid, as output times snapped to frame boundaries.

    Normal bars cut every `every` beats (counted from the downbeat), high-energy
    bars twice as often, quiet bars half as often.
    """
    steps = {1: max(1, every // 2), 0: every, -1: every * 2}
    cuts: list[float] = []
    last = 0.0
    for i, t in enumerate(analysis.beats):
        rel = float(t) - start
        if rel < 0:
            continue
        if rel > duration - MIN_GAP:
            break
        if (i - analysis.phase) % steps[analysis.level_of(i)]:
            continue
        snapped = round(rel * fps) / fps
        if snapped - last < MIN_GAP:
            continue
        cuts.append(snapped)
        last = snapped
    return cuts


def clean_cuts(cuts: list[float], duration: float, fps: int) -> list[float]:
    """Snap hand-placed cuts to frames, sort them, and drop any that crowd a neighbour."""
    out: list[float] = []
    last = 0.0
    for t in sorted(round(float(c) * fps) / fps for c in cuts):
        if t - last < MIN_GAP or t > duration - MIN_GAP:
            continue
        out.append(t)
        last = t
    return out


def slot_energy(analysis: Analysis, edges: list[float], start: float) -> list[tuple[float, int]]:
    """(strength, level) of the music under each slot. Level is +1 loud, 0, -1 quiet."""
    beats = analysis.beats
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        i = int(np.searchsorted(beats, start + a - 0.02))
        j = max(i + 1, int(np.searchsorted(beats, start + b - 0.02)))
        i = min(i, len(beats) - 1)
        strength = float(analysis.beat_energy[i:j].mean())
        on_downbeat = abs(float(beats[i]) - (start + a)) < 0.05 and analysis.is_downbeat(i)
        out.append((strength * (DOWNBEAT_BONUS if on_downbeat else 1.0), analysis.level_of(i)))
    return out


# ---------- footage ----------


@dataclass
class _Footage:
    """Tracks which parts of each clip are already taken."""

    clips: list[Clip]
    opts: Options
    used: list[list[tuple[float, float]]] = field(default_factory=list)
    used_secs: list[float] = field(default_factory=list)

    def __post_init__(self):
        self.used = [[] for _ in self.clips]
        self.used_secs = [0.0] * len(self.clips)
        tops = [float(c.scores(self.opts.pick).max()) for c in self.clips if len(c.motion)]
        self.top = max(tops, default=0.0) or 1.0

    def take(self, ci: int, start: float, dur: float) -> None:
        self.used[ci].append((start, start + dur))
        self.used_secs[ci] += dur

    def is_free(self, ci: int, start: float, dur: float) -> bool:
        clip = self.clips[ci]
        if start < 0 or start + dur > clip.duration + 1e-6:
            return False
        return all(not (start < b and start + dur > a) for a, b in self.used[ci])

    def best_window(self, ci: int, dur: float, rng: np.random.Generator, calm: bool = False):
        """Best unused window of length `dur` in a clip, or None. Returns (start, score)."""
        clip = self.clips[ci]
        values = clip.scores(self.opts.pick)
        n = max(1, int(np.ceil(dur * RATE)))
        count = int(np.floor((clip.duration - dur) * RATE + 1e-6)) + 1
        count = min(count, len(values) - n + 1)
        if count <= 0:
            return None
        csum = np.concatenate([[0.0], np.cumsum(values, dtype=np.float64)])
        scores = (csum[n : n + count] - csum[:count]) / n
        starts = np.arange(count) / RATE
        free = np.ones(count, dtype=bool)
        for a, b in self.used[ci]:
            free &= ~((starts < b) & (starts + dur > a))
        # Keep away from the wobbly first and last moments when the clip is long enough to allow it.
        trim = self.opts.trim_edges
        if trim > 0:
            inner = free & (starts >= trim) & (starts + dur <= clip.duration - trim)
            if inner.any():
                free = inner
        if not free.any():
            return None
        # Tiny jitter so ties (static footage) are broken by the seed, not by position.
        wanted = -scores if calm else scores
        ranked = np.where(free, wanted + rng.random(count) * 1e-6 * (1.0 + scores.max()), -np.inf)
        best = int(np.argmax(ranked))
        return float(starts[best]), float(scores[best])

    def rank(self, ci: int, score: float, rng: np.random.Generator, calm: bool = False) -> float:
        strength = score / self.top
        spread = USAGE_PENALTY * self.used_secs[ci] / self.clips[ci].duration
        return (-strength if calm else strength) - spread + rng.random() * 1e-3


def _segment(footage: _Footage, ci: int, start: float, end: float, clip_start: float, score: float, locked=False):
    clip = footage.clips[ci]
    cx, cy = clip.focus(clip_start, end - start) if footage.opts.crop == "motion" else (0.5, 0.5)
    return Segment(start, end, clip.path, clip_start, score, cx, cy, locked)


def _window_score(clip: Clip, pick: str, start: float, dur: float) -> float:
    a = int(start * RATE)
    part = clip.scores(pick)[a : max(a + 1, int(np.ceil((start + dur) * RATE)))]
    return float(part.mean()) if len(part) else 0.0


def assign(
    cuts: list[float],
    duration: float,
    clips: list[Clip],
    opts: Options,
    energy: list[tuple[float, int]] | None = None,
    locks: list[Lock] | None = None,
    skip_slot: int | None = None,
) -> tuple[list[Segment | None], _Footage]:
    """Fill each slot between cuts with unused footage.

    Locked slots are honoured first. The rest are filled in time order, or, with
    opts.match_energy, loudest slot first so the strongest footage lands on the
    strongest music. `skip_slot` leaves one slot empty (used to list alternatives).
    """
    rng = np.random.default_rng(opts.seed)
    edges = [0.0, *cuts, duration]
    slots = list(zip(edges[:-1], edges[1:]))
    footage = _Footage(clips, opts)
    index = {c.path.name: i for i, c in enumerate(clips)}
    chosen: list[tuple[int, Segment] | None] = [None] * len(slots)

    for lock in locks or []:
        si = next((i for i, (a, _) in enumerate(slots) if abs(a - lock.start) < 0.02), None)
        ci = index.get(lock.clip)
        if si is None or ci is None or chosen[si] is not None or si == skip_slot:
            continue
        a, b = slots[si]
        if not footage.is_free(ci, lock.clip_start, b - a):
            continue  # the slot grew past the end of the clip, or overlaps another lock
        footage.take(ci, lock.clip_start, b - a)
        score = _window_score(clips[ci], opts.pick, lock.clip_start, b - a)
        chosen[si] = (ci, _segment(footage, ci, a, b, lock.clip_start, score, locked=True))

    order = [i for i in range(len(slots)) if chosen[i] is None and i != skip_slot]
    use_energy = opts.match_energy and energy is not None
    if use_energy:
        order.sort(key=lambda i: -energy[i][0])

    for si in order:
        a, b = slots[si]
        calm = use_energy and energy[si][1] < 0
        options = []
        for ci in range(len(clips)):
            found = footage.best_window(ci, b - a, rng, calm)
            if found is not None:
                options.append((footage.rank(ci, found[1], rng, calm), ci, *found))
        if not options:
            have = sum(c.duration for c in clips)
            raise NotEnoughFootage(
                f"Ran out of unused footage at {a:.1f}s of {duration:.1f}s "
                f"({have:.1f}s of clips in total). Add clips, shorten the edit or cut less often."
            )
        # Avoid back-to-back picks from one clip when there is a choice.
        beside = {chosen[j][0] for j in (si - 1, si + 1) if 0 <= j < len(slots) and chosen[j] is not None}
        fresh = [o for o in options if o[1] not in beside] or options
        _, ci, clip_start, score = max(fresh)
        footage.take(ci, clip_start, b - a)
        chosen[si] = (ci, _segment(footage, ci, a, b, clip_start, score))

    return [c[1] if c else None for c in chosen], footage


def build(cuts, duration, clips, opts, energy=None, locks=None) -> list[Segment]:
    segments, _ = assign(cuts, duration, clips, opts, energy, locks)
    return segments


def alternatives(slot, cuts, duration, clips, opts, energy=None, locks=None, limit: int = 8) -> list[Segment]:
    """Other footage that could fill one slot: the best unused window of each clip, strongest first."""
    edges = [0.0, *cuts, duration]
    if not 0 <= slot < len(edges) - 1:
        return []
    _, footage = assign(cuts, duration, clips, opts, energy, locks, skip_slot=slot)
    a, b = edges[slot], edges[slot + 1]
    rng = np.random.default_rng(opts.seed + 7919 * (slot + 1))
    found = []
    for ci in range(len(clips)):
        window_ = footage.best_window(ci, b - a, rng)
        if window_ is not None:
            found.append((window_[1], ci, window_[0]))
    found.sort(reverse=True)
    return [_segment(footage, ci, a, b, clip_start, score) for score, ci, clip_start in found[:limit]]
