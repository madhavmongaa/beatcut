"""End-to-end test on synthetic media: cuts must land within 40 ms of the 120 BPM beats."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from beatcut import cli

from conftest import BEAT

TOLERANCE = 0.040
FPS = 30


def off_beat(times) -> np.ndarray:
    """Distance from each time to the nearest true beat, in seconds."""
    times = np.asarray(times, dtype=float)
    return np.abs(times - np.round(times / BEAT) * BEAT)


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-count_frames", str(path)],
        check=True, capture_output=True,
    ).stdout
    return json.loads(out)


def rendered_cuts(path: Path) -> list[float]:
    """Times of hard cuts in a rendered file, found from frame-to-frame colour change."""
    w, h = 32, 32
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={w}:{h}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    ).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, h, w, 3).astype(np.int16)
    change = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2, 3))
    return [(i + 1) / FPS for i in np.flatnonzero(change > 12)]


@pytest.fixture(scope="module")
def edit(click_track, clips_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("out") / "final.mp4"
    code = cli.main(["--clips", str(clips_dir), "--track", str(click_track), "--out", str(out), "--seed", "7"])
    assert code == 0
    return out, json.loads(out.with_suffix(".cuts.json").read_text())


def test_tempo_and_downbeats(edit):
    _, data = edit
    assert data["analysis"]["tempo"] == pytest.approx(120, abs=2)
    # Downbeats are the accented clicks, every 4 beats starting at 0.
    downbeats = np.array(data["analysis"]["downbeats"])
    assert np.all(np.abs(downbeats - np.round(downbeats / (4 * BEAT)) * 4 * BEAT) <= TOLERANCE)


def test_planned_cuts_land_on_beats(edit):
    _, data = edit
    cuts = data["cuts"]
    # 30 s at 120 BPM, one cut every 2 beats: a cut each second from 1 to 29.
    assert len(cuts) == 29
    assert off_beat(cuts).max() <= TOLERANCE
    assert np.allclose(np.diff(cuts), 2 * BEAT, atol=TOLERANCE)


def test_rendered_cuts_land_on_beats(edit):
    out, data = edit
    found = rendered_cuts(out)
    assert len(found) == len(data["cuts"])
    assert off_beat(found).max() <= TOLERANCE
    assert np.allclose(found, data["cuts"], atol=1 / FPS)


def test_output_format(edit):
    out, _ = edit
    streams = probe(out)["streams"]
    video = [s for s in streams if s["codec_type"] == "video"]
    audio = [s for s in streams if s["codec_type"] == "audio"]
    assert len(video) == 1 and len(audio) == 1  # the track is the only audio
    assert (video[0]["width"], video[0]["height"]) == (1080, 1920)
    assert video[0]["r_frame_rate"] == "30/1"
    assert int(video[0]["nb_read_frames"]) == 30 * FPS
    assert float(audio[0]["duration"]) == pytest.approx(30, abs=0.1)


def test_mixed_sources_give_one_uniform_stream(edit):
    """HDR, 60 fps and odd-sized clips must all come out as the same SDR stream."""
    out, data = edit
    assert "clip6.mp4" in {s["clip"] for s in data["segments"]}  # the HDR clip is in the edit
    rows = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "frame=width,height,pix_fmt,color_transfer,color_space", "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert len(rows) == 30 * FPS
    assert {r.rstrip(",") for r in rows} == {"1080,1920,yuv420p,bt709,bt709"}


def test_no_segment_reused(edit):
    _, data = edit
    by_clip: dict[str, list[tuple[float, float]]] = {}
    for seg in data["segments"]:
        length = seg["end"] - seg["start"]
        by_clip.setdefault(seg["clip"], []).append((seg["clip_start"], seg["clip_start"] + length))
    for spans in by_clip.values():
        spans.sort()
        for (_, end), (start, _) in zip(spans, spans[1:]):
            assert start >= end - 1e-6


def test_seed_is_reproducible(click_track, clips_dir, edit, tmp_path):
    _, first = edit
    out = tmp_path / "again.mp4"
    assert cli.main(["--clips", str(clips_dir), "--track", str(click_track), "--out", str(out), "--seed", "7"]) == 0
    again = json.loads(out.with_suffix(".cuts.json").read_text())
    assert again["segments"] == first["segments"]


@pytest.mark.parametrize("aspect,size", [("1:1", (1080, 1080)), ("16:9", (1920, 1080))])
def test_aspect_and_every(click_track, clips_dir, tmp_path, aspect, size):
    out = tmp_path / "short.mp4"
    args = ["--clips", str(clips_dir), "--track", str(click_track), "--out", str(out)]
    assert cli.main([*args, "--aspect", aspect, "--every", "4", "--duration", "8", "--seed", "1"]) == 0
    data = json.loads(out.with_suffix(".cuts.json").read_text())
    assert data["cuts"] == pytest.approx([2.0, 4.0, 6.0], abs=TOLERANCE)
    video = next(s for s in probe(out)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == size
