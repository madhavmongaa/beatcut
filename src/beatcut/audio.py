"""Track analysis: tempo, beats, downbeats and per-bar energy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np

from . import ffutil

SR = 22050
HOP = 256  # ~11.6 ms per frame
METERS = (3, 4, 6)  # beats per bar we can count in

# A bar counts as high / quiet energy when its onset strength is this far from the median.
HIGH_RATIO = 1.3
QUIET_RATIO = 0.7


@dataclass
class Analysis:
    duration: float
    tempo: float
    beats: np.ndarray  # beat times in seconds
    phase: int  # index of the first downbeat within `beats`
    beat_energy: np.ndarray  # mean onset strength from each beat to the next
    bar_levels: dict[int, int]  # bar index -> +1 high, 0 normal, -1 quiet
    meter: int = 4  # beats per bar

    @property
    def downbeats(self) -> np.ndarray:
        return self.beats[self.phase :: self.meter]

    def bar_of(self, beat_index: int) -> int:
        return (beat_index - self.phase) // self.meter

    def is_downbeat(self, beat_index: int) -> bool:
        return (beat_index - self.phase) % self.meter == 0

    def level_of(self, beat_index: int) -> int:
        return self.bar_levels.get(self.bar_of(beat_index), 0)

    def to_json(self) -> dict:
        return {
            "duration": round(self.duration, 4),
            "tempo": round(self.tempo, 3),
            "meter": self.meter,
            "beats": [round(float(t), 4) for t in self.beats],
            "downbeats": [round(float(t), 4) for t in self.downbeats],
            "beat_levels": [self.level_of(i) for i in range(len(self.beats))],
        }


def load(path: Path) -> np.ndarray:
    """Decode any audio ffmpeg can read to mono float32 at SR."""
    raw = ffutil.ffmpeg(
        ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture=True,
    )
    y = np.frombuffer(raw, dtype=np.float32)
    if y.size < SR:
        raise ValueError(f"{path} has less than a second of audio")
    return y


def _refine(beats: np.ndarray, y: np.ndarray, window: float = 0.05) -> np.ndarray:
    """Nudge each beat onto the sharpest energy rise within +/- window seconds.

    The beat tracker works on a smoothed envelope and can sit a few tens of
    milliseconds off the transient. A fine-grained energy envelope pins it down.
    """
    hop = 32
    rms = librosa.feature.rms(y=y, frame_length=256, hop_length=hop, center=True)[0]
    rise = np.maximum(0.0, np.diff(np.log1p(1000.0 * rms), prepend=0.0))
    span = int(round(window * SR / hop))
    out = beats.copy()
    for i, t in enumerate(beats):
        c = int(round(t * SR / hop))
        lo, hi = max(0, c - span), min(len(rise), c + span + 1)
        if hi - lo < 3:
            continue
        seg = rise[lo:hi]
        peak = int(np.argmax(seg))
        # Only move when there is a clear transient; otherwise trust the tracker.
        if seg[peak] > 4.0 * (np.mean(seg) + 1e-9):
            out[i] = (lo + peak) * hop / SR
    return out


def _extend(beats: np.ndarray, duration: float) -> np.ndarray:
    """Continue the beat grid to both ends of the track.

    The tracker drops beats in the intro and outro; without them the first cut
    could land several seconds in.
    """
    head = float(np.median(np.diff(beats[:8])))
    tail = float(np.median(np.diff(beats[-8:])))
    before, after = [], []
    t = beats[0] - head
    while t > -0.02:
        before.append(max(t, 0.0))
        t -= head
    t = beats[-1] + tail
    while t < duration:
        after.append(t)
        t += tail
    return np.concatenate([before[::-1], beats, after])


def _levels(values: np.ndarray) -> np.ndarray:
    med = float(np.median(values))
    if med <= 0:
        return np.zeros(len(values), dtype=int)
    ratio = values / med
    return np.where(ratio > HIGH_RATIO, 1, np.where(ratio < QUIET_RATIO, -1, 0))


def analyse(path: Path, meter: int = 4) -> Analysis:
    y = load(path)
    duration = len(y) / SR
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP)
    tempo, frames = librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=HOP)
    tempo = float(np.atleast_1d(tempo)[0])
    if len(frames) < 4:
        raise ValueError("Could not find a steady beat in the track")
    beats = librosa.frames_to_time(frames, sr=SR, hop_length=HOP)
    beats = _refine(_extend(beats, duration), y)

    # Energy from each beat up to the next one. This uses onset strength on a linear
    # amplitude scale: the default envelope is log-compressed, which makes a quiet intro
    # score almost the same as a loud chorus.
    mel = librosa.feature.melspectrogram(y=y, sr=SR, hop_length=HOP, power=1.0)
    loud = librosa.onset.onset_strength(S=mel, sr=SR, hop_length=HOP)
    edges = np.append(librosa.time_to_frames(beats, sr=SR, hop_length=HOP), len(env))
    beat_energy = np.array([loud[a:b].mean() if b > a else 0.0 for a, b in zip(edges[:-1], edges[1:])])

    # Downbeat phase: take the offset within the bar whose beats hit hardest.
    at_beat = np.array([env[max(0, f - 2) : f + 3].max() for f in edges[:-1]])
    phase = int(np.argmax([at_beat[p::meter].mean() for p in range(meter)]))

    # Per-bar energy, lightly smoothed over neighbouring bars so sections hold together.
    bars: dict[int, list[float]] = {}
    for i, e in enumerate(beat_energy):
        bars.setdefault((i - phase) // meter, []).append(float(e))
    keys = sorted(bars)
    raw = np.array([np.mean(bars[k]) for k in keys])
    smooth = np.convolve(np.pad(raw, 1, mode="edge"), [0.25, 0.5, 0.25], mode="valid")
    bar_levels = {k: int(v) for k, v in zip(keys, _levels(smooth))}

    return Analysis(duration, tempo, beats, phase, beat_energy, bar_levels, meter)


def best_start(analysis: Analysis, length: float) -> float:
    """The downbeat that opens the highest-energy stretch of `length` seconds."""
    beats, energy = analysis.beats, analysis.beat_energy
    best, best_score = 0.0, -1.0
    for i in range(analysis.phase, len(beats), analysis.meter):
        t = float(beats[i])
        if t + length > analysis.duration:
            break
        j = int(np.searchsorted(beats, t + length))
        score = float(energy[i:j].mean()) if j > i else 0.0
        if score > best_score:
            best, best_score = t, score
    return round(best, 3)
