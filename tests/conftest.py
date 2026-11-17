"""Synthetic fixtures: a 120 BPM click track and colour-bar clips, all made with ffmpeg."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

BPM = 120
BEAT = 60 / BPM  # 0.5 s
TRACK_SECONDS = 40
CLIP_SECONDS = 10

# Eight visually distinct sources, so every cut in the output is a visible change.
SOURCES = [
    "smptebars",
    "smptehdbars",
    "pal75bars",
    "pal100bars",
    "testsrc2",
    "testsrc",
    "rgbtestsrc",
    "smptebars,negate",
]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def click_track(tmp_path_factory) -> Path:
    """A click on every beat at 120 BPM, with a louder, higher click on each downbeat."""
    path = tmp_path_factory.mktemp("audio") / "click.wav"
    downbeat = f"lt(mod(t,{4 * BEAT}),{BEAT})"
    expr = f"(0.35+0.45*{downbeat})*sin(2*PI*(1000+600*{downbeat})*t)*exp(-70*mod(t,{BEAT}))"
    _ffmpeg("-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d={TRACK_SECONDS}", str(path))
    return path


@pytest.fixture(scope="session")
def clips_dir(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("clips")
    for i, source in enumerate(SOURCES):
        name, *filters = source.split(",")
        # Mix formats the way a real camera roll does: one clip is 60 fps at another
        # size, one is 10-bit HEVC tagged as HDR (HLG) like iPhone footage.
        size, rate = ("1280x720", 60) if i == 5 else ("640x360", 30)
        graph = ",".join([f"{name}=size={size}:rate={rate}:duration={CLIP_SECONDS}", *filters])
        if i == 6:
            codec = ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
                     "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc"]
        else:
            codec = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        _ffmpeg("-f", "lavfi", "-i", graph, *codec, str(folder / f"clip{i}.mp4"))
    return folder
