"""The optional behaviours: start point, meter, sharp picking, energy matching,
motion crop, edge trimming, the hardware encoder, and locked shots."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from beatcut import audio, cli, plan, render
from beatcut.motion import RATE, H, W, Clip

from conftest import BEAT
from test_synthetic import FPS, TOLERANCE, off_beat, rendered_cuts


def run(click_track, clips_dir, out: Path, *extra: str) -> dict:
    args = ["--clips", str(clips_dir), "--track", str(click_track), "--out", str(out), "--seed", "4", *extra]
    assert cli.main(args) == 0
    return json.loads(out.with_suffix(".cuts.json").read_text())


def fake_clip(name: str, seconds: float, motion, sharp=None, cols=None) -> Clip:
    n = int(seconds * RATE)
    motion = np.broadcast_to(np.asarray(motion, np.float32), (n,)).copy()
    sharp = motion if sharp is None else np.broadcast_to(np.asarray(sharp, np.float32), (n,)).copy()
    cols = np.ones((n, W), np.float32) if cols is None else np.tile(np.asarray(cols, np.float32), (n, 1))
    return Clip(Path(name), seconds, motion, sharp, cols, np.ones((n, H), np.float32))


# ---------- start point ----------


def test_start_offset_keeps_cuts_on_the_beat(click_track, clips_dir, tmp_path):
    # Start a quarter of a beat late: cuts must follow the music, not the output clock.
    data = run(click_track, clips_dir, tmp_path / "o.mp4", "--start", "4.125", "--duration", "8")
    assert data["start"] == pytest.approx(4.125)
    in_track = np.array(data["cuts"]) + data["start"]
    assert len(in_track) >= 3
    assert off_beat(in_track).max() <= TOLERANCE
    found = np.array(rendered_cuts(tmp_path / "o.mp4")) + data["start"]
    assert off_beat(found).max() <= TOLERANCE


def test_start_offset_moves_the_audio(click_track, clips_dir, tmp_path):
    run(click_track, clips_dir, tmp_path / "o.mp4", "--start", "4.125", "--duration", "4")
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(tmp_path / "o.mp4"), "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
        check=True, capture_output=True,
    ).stdout
    y = np.abs(np.frombuffer(raw, np.float32))
    first_click = np.argmax(y > 0.2 * y.max()) / 8000  # low threshold: off-beat clicks are quieter
    # The next click after 4.125 s in the track is at 4.5 s, so 0.375 s into the output.
    assert first_click == pytest.approx(0.375, abs=TOLERANCE)


def test_auto_start_finds_the_loud_part(tmp_path):
    # Quiet clicks for 16 s, then loud ones: the loudest 8 s must start in the loud half.
    path = tmp_path / "two_halves.wav"
    expr = f"(0.05+0.85*gte(t,16))*sin(2*PI*1000*t)*exp(-70*mod(t,{BEAT}))"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d=32", str(path)], check=True)
    analysis = audio.analyse(path)
    start, duration = plan.window(analysis, "auto", 8, FPS)
    assert 16 - TOLERANCE <= start <= 24 + TOLERANCE
    assert duration == pytest.approx(8)


def test_window_is_clamped_to_the_track(click_track):
    analysis = audio.analyse(click_track)
    start, duration = plan.window(analysis, 35, 30, FPS)
    assert start == 35 and duration == pytest.approx(5, abs=0.05)


# ---------- meter ----------


def test_meter_three_puts_downbeats_every_three_beats(tmp_path):
    path = tmp_path / "waltz.wav"
    down = f"lt(mod(t,{3 * BEAT}),{BEAT})"
    expr = f"(0.3+0.6*{down})*sin(2*PI*(1000+600*{down})*t)*exp(-70*mod(t,{BEAT}))"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d=30", str(path)], check=True)
    analysis = audio.analyse(path, meter=3)
    downbeats = analysis.downbeats
    assert np.all(np.abs(downbeats - np.round(downbeats / (3 * BEAT)) * 3 * BEAT) <= TOLERANCE)
    # Cutting every 3 beats in 3/4 lands every cut on a downbeat.
    cuts = np.array(plan.cut_times(analysis, 3, 12, FPS))
    assert len(cuts) == 7
    assert np.all(np.abs(cuts - np.round(cuts / (3 * BEAT)) * 3 * BEAT) <= TOLERANCE)


# ---------- choosing footage ----------


def test_sharp_prefers_steady_action_over_blur():
    blur = fake_clip("blur.mp4", 10, motion=40, sharp=2)  # lots of change, all of it smear
    steady = fake_clip("steady.mp4", 10, motion=15, sharp=14)
    by_motion = plan.build([], 2.0, [blur, steady], plan.Options(pick="motion"))
    by_sharp = plan.build([], 2.0, [blur, steady], plan.Options(pick="sharp"))
    assert by_motion[0].clip.name == "blur.mp4"
    assert by_sharp[0].clip.name == "steady.mp4"


def test_match_energy_gives_the_loudest_slot_the_strongest_footage():
    ramp = np.linspace(1, 50, 200)  # one clip whose action builds over 20 s
    clips = [fake_clip("ramp.mp4", 20, motion=ramp), fake_clip("flat.mp4", 20, motion=5)]
    cuts = [2.0, 4.0, 6.0]
    energy = [(1.0, 0), (1.0, 0), (9.0, 1), (0.2, -1)]  # third slot is the loud one, fourth is quiet
    in_order = plan.build(cuts, 8.0, clips, plan.Options(), energy)
    matched = plan.build(cuts, 8.0, clips, plan.Options(match_energy=True), energy)
    assert max(matched, key=lambda s: s.motion) is matched[2]
    assert max(in_order, key=lambda s: s.motion) is in_order[0]
    assert matched[3].motion <= min(s.motion for s in matched[:3])  # the quiet slot gets the calm footage


def test_trim_edges_avoids_clip_starts_and_ends(click_track, clips_dir, tmp_path):
    data = run(click_track, clips_dir, tmp_path / "o.mp4", "--trim-edges", "0.5", "--duration", "10")
    for seg in data["segments"]:
        assert seg["clip_start"] >= 0.5 - 1e-6
        assert seg["clip_start"] + seg["end"] - seg["start"] <= 10 - 0.5 + 1e-6


def test_motion_crop_follows_the_action():
    right = np.zeros(W)
    right[int(W * 0.8):] = 30  # all the change is on the right of the frame
    clip = fake_clip("right.mp4", 10, motion=10, cols=right)
    centred = plan.build([], 2.0, [clip], plan.Options(crop="centre"))[0]
    follow = plan.build([], 2.0, [clip], plan.Options(crop="motion"))[0]
    assert (centred.crop_x, centred.crop_y) == (0.5, 0.5)
    assert follow.crop_x == pytest.approx(0.9, abs=0.02)
    assert follow.crop_y == 0.5  # no vertical preference in this clip


def test_all_options_together(click_track, clips_dir, tmp_path):
    out = tmp_path / "o.mp4"
    data = run(click_track, clips_dir, out, "--start", "auto", "--pick", "sharp", "--match-energy",
               "--crop", "motion", "--trim-edges", "0.5", "--duration", "8", "--aspect", "1:1")
    assert off_beat(np.array(data["cuts"]) + data["start"]).max() <= TOLERANCE
    assert len(rendered_cuts(out)) == len(data["cuts"])
    spans: dict[str, list] = {}
    for seg in data["segments"]:
        assert 0 <= seg["crop_x"] <= 1 and 0 <= seg["crop_y"] <= 1
        spans.setdefault(seg["clip"], []).append((seg["clip_start"], seg["clip_start"] + seg["end"] - seg["start"]))
    for used in spans.values():
        used.sort()
        assert all(b[0] >= a[1] - 1e-6 for a, b in zip(used, used[1:]))  # still no reuse


# ---------- hardware encoder ----------


@pytest.mark.skipif(not render.hardware_available(), reason="no VideoToolbox H.264 encoder")
def test_hardware_encoder_output(click_track, clips_dir, tmp_path):
    out = tmp_path / "o.mp4"
    data = run(click_track, clips_dir, out, "--encoder", "hardware", "--duration", "8")
    assert data["encoder"] == "hardware"
    found = rendered_cuts(out)
    assert len(found) == len(data["cuts"]) and off_beat(found).max() <= TOLERANCE
    frames = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True,
    ).stdout.strip().rstrip(",")
    assert int(frames) == 8 * FPS


# ---------- locked shots ----------


def test_locks_are_honoured_and_never_reused():
    clips = [fake_clip("a.mp4", 10, motion=20), fake_clip("b.mp4", 10, motion=10)]
    cuts = [2.0, 4.0, 6.0]
    lock = plan.Lock(start=4.0, clip="b.mp4", clip_start=3.0)
    for seed in range(5):
        segs = plan.build(cuts, 8.0, clips, plan.Options(seed=seed), locks=[lock])
        assert (segs[2].clip.name, segs[2].clip_start, segs[2].locked) == ("b.mp4", 3.0, True)
        for other in segs[:2] + segs[3:]:
            assert not other.locked
            overlaps = other.clip.name == "b.mp4" and other.clip_start < 5.0 and other.clip_start + 2.0 > 3.0
            assert not overlaps


def test_a_lock_that_no_longer_fits_is_dropped():
    clips = [fake_clip("a.mp4", 30, motion=20), fake_clip("b.mp4", 3, motion=10)]
    lock = plan.Lock(start=0.0, clip="b.mp4", clip_start=2.0)  # 2 s in a 3 s clip leaves 1 s, the slot needs 4
    segs = plan.build([4.0], 8.0, clips, plan.Options(), locks=[lock])
    assert not any(s.locked for s in segs)


def test_alternatives_only_offer_unused_footage():
    clips = [fake_clip("a.mp4", 20, motion=20), fake_clip("b.mp4", 20, motion=10), fake_clip("c.mp4", 20, motion=5)]
    cuts = [2.0, 4.0]
    opts = plan.Options(seed=1)
    segs = plan.build(cuts, 6.0, clips, opts)
    options = plan.alternatives(1, cuts, 6.0, clips, opts)
    assert {o.clip.name for o in options} == {"a.mp4", "b.mp4", "c.mp4"}
    assert [o.motion for o in options] == sorted((o.motion for o in options), reverse=True)
    for opt in options:
        for other in (segs[0], segs[2]):
            same = opt.clip == other.clip
            assert not (same and opt.clip_start < other.clip_start + 2.0 and opt.clip_start + 2.0 > other.clip_start)
