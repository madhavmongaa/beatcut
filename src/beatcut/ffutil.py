"""Thin subprocess helpers around ffmpeg / ffprobe."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

# Hardware decoding makes 4K phone footage several times faster to read on a Mac.
# ffmpeg falls back to software decoding by itself when a file is not supported.
HWACCEL = ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" else []


class FFmpegError(RuntimeError):
    pass


def require() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise FFmpegError(f"{tool} not found on PATH. Install it with: brew install ffmpeg")


def run(args: list[str], capture: bool = False) -> bytes:
    """Run a command, returning stdout bytes when capture=True. Raises on failure."""
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-8:]
        raise FFmpegError(f"{args[0]} failed ({proc.returncode}):\n" + "\n".join(tail))
    return proc.stdout if capture else b""


def ffmpeg(args: list[str], capture: bool = False) -> bytes:
    return run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args], capture)


def probe(path: Path) -> dict:
    out = run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture=True,
    )
    return json.loads(out)


def duration(path: Path) -> float:
    """Duration in seconds, preferring the video stream when there is one."""
    info = probe(path)
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video" and "duration" in stream:
            return float(stream["duration"])
    return float(info["format"]["duration"])


def has_video(path: Path) -> bool:
    return any(s.get("codec_type") == "video" for s in probe(path).get("streams", []))
