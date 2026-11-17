"""Rendering: cut each segment with ffmpeg, join them, and lay the track underneath."""

from __future__ import annotations

import functools
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffutil
from .plan import Segment

ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}
ENCODERS = ("x264", "hardware")
FADE_OUT = 0.25  # seconds of audio fade at the end


@functools.cache
def hardware_available() -> bool:
    """True when ffmpeg can encode H.264 with the Mac's VideoToolbox."""
    try:
        ffutil.ffmpeg(["-f", "lavfi", "-i", "color=black:s=640x360:d=0.2", "-c:v", "h264_videotoolbox", "-f", "null", "-"])
    except ffutil.FFmpegError:
        return False
    return True


def _codec_args(encoder: str, draft: bool) -> list[str]:
    if encoder == "hardware":
        # About a third faster than x264 on a full-resolution export of 4K phone clips.
        return ["-c:v", "h264_videotoolbox", "-q:v", "45" if draft else "62", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "ultrafast" if draft else "veryfast", "-crf", "26" if draft else "18"]


def _cut_segment(seg: Segment, dest: Path, size: tuple[int, int], fps: int, codec: list[str]) -> None:
    w, h = size
    frames = round((seg.end - seg.start) * fps)
    # Convert everything to standard-range BT.709 while scaling. Phone footage is often
    # HDR (HLG); left alone it looks washed out and changes the stream's colour settings
    # from one segment to the next.
    vf = (
        f"fps={fps},"
        f"scale={w}:{h}:force_original_aspect_ratio=increase"
        ":out_color_matrix=bt709:out_primaries=bt709:out_transfer=bt709:out_range=tv,"
        # Crop around (crop_x, crop_y), kept inside the frame. 0.5, 0.5 is a centre crop.
        f"crop={w}:{h}:x='clip({seg.crop_x}*iw-ow/2,0,iw-ow)':y='clip({seg.crop_y}*ih-oh/2,0,ih-oh)',"
        "setsar=1,format=yuv420p"
    )
    ffutil.ffmpeg([
        *ffutil.HWACCEL, "-ss", f"{seg.clip_start:.3f}", "-i", str(seg.clip),
        "-an", "-vf", vf, "-frames:v", str(frames),
        *codec,
        # No B-frames and a fixed timescale so segments join without timestamp drift.
        "-bf", "0", "-video_track_timescale", "15360",
        # Identical stream parameters in every segment, whatever the source was.
        "-profile:v", "high", "-level", "4.2",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        str(dest),
    ])


def render(
    segments: list[Segment],
    track: Path,
    out: Path,
    size: tuple[int, int],
    fps: int,
    duration: float,
    progress=None,
    start: float = 0.0,
    encoder: str = "x264",
    draft: bool = False,
) -> str:
    """Render the edit. Returns the encoder actually used."""
    if encoder == "hardware" and not hardware_available():
        encoder = "x264"
    codec = _codec_args(encoder, draft)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="beatcut-") as tmp:
        tmpdir = Path(tmp)
        parts = [tmpdir / f"seg{i:04d}.mp4" for i in range(len(segments))]

        def work(job):
            seg, dest = job
            _cut_segment(seg, dest, size, fps, codec)
            if progress:
                progress()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(work, zip(segments, parts)))

        listing = tmpdir / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        fade_start = max(0.0, duration - FADE_OUT)
        ffutil.ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-ss", f"{start:.3f}", "-t", f"{duration:.4f}", "-i", str(track),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-af", f"afade=t=out:st={fade_start:.4f}:d={FADE_OUT}",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{duration:.4f}", "-movflags", "+faststart",
            str(out),
        ])
    return encoder
