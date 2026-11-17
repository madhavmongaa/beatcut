"""beatcut command line."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

from . import audio, ffutil, motion, plan, render

FPS = 30


def make_edit(
    clips_dir: Path,
    track: Path,
    out: Path,
    duration: float = 30.0,
    every: int = 2,
    aspect: str = "9:16",
    seed: int | None = None,
    start: float | str = 0.0,
    meter: int = 4,
    pick: str = "motion",
    match_energy: bool = False,
    crop: str = "centre",
    trim_edges: float = 0.0,
    encoder: str = "x264",
    log=lambda msg: None,
) -> dict:
    """Run the whole pipeline. Returns the edit description that is also written next to `out`."""
    ffutil.require()
    if seed is None:
        seed = secrets.randbelow(2**31)
    size = render.ASPECTS[aspect]
    opts = plan.Options(seed=seed, pick=pick, match_energy=match_energy, crop=crop, trim_edges=trim_edges)

    log(f"Analysing {track.name}")
    analysis = audio.analyse(track, meter)
    wanted = duration
    start, duration = plan.window(analysis, start, wanted, FPS)
    if duration < wanted - 1 / FPS:
        log(f"Only {duration:.1f}s of track from {start:.1f}s; shortening the edit to match")
    log(f"  {analysis.tempo:.1f} BPM, {len(analysis.beats)} beats, starting at {start:.2f}s")

    paths = motion.find_clips(clips_dir)
    log(f"Scoring motion in {len(paths)} clips")
    clips = [c for c in motion.analyse_clips(paths) if c.duration > 0]

    cuts = plan.cut_times(analysis, every, duration, FPS, start)
    energy = plan.slot_energy(analysis, [0.0, *cuts, duration], start)
    segments = plan.build(cuts, duration, clips, opts, energy)
    log(f"Rendering {len(segments)} segments at {size[0]}x{size[1]} (seed {seed})")

    done = [0]

    def tick():
        done[0] += 1
        print(f"\r  {done[0]}/{len(segments)}", end="", file=sys.stderr, flush=True)

    used = render.render(
        segments, track, out, size, FPS, duration,
        progress=tick if log is _stderr else None, start=start, encoder=encoder,
    )
    if log is _stderr:
        print(file=sys.stderr)
    if used != encoder:
        log("Hardware encoding is not available here; used x264 instead")

    edit = {
        "track": track.name,
        "fps": FPS,
        "size": list(size),
        "start": start,
        "duration": round(duration, 4),
        "every": every,
        "encoder": used,
        **opts.to_json(),
        "cuts": [round(c, 4) for c in cuts],
        "segments": [s.to_json() for s in segments],
        "analysis": analysis.to_json(),
    }
    sidecar = out.with_suffix(".cuts.json")
    sidecar.write_text(json.dumps(edit, indent=2))
    log(f"Wrote {out} and {sidecar.name}")
    return edit


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def _start(text: str) -> float | str:
    if text == "auto":
        return text
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more, or 'auto'")
    return value


def _seconds(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _ui(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="beatcut ui", description="Open the local beatcut web UI.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project", type=Path, default=Path.home() / ".beatcut" / "project",
                        help="folder that holds the project's media and settings (default ~/.beatcut/project)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser window")
    args = parser.parse_args(argv)
    from . import web

    web.serve(args.port, args.project, open_browser=not args.no_open)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["ui"]:
        return _ui(argv[1:])
    parser = argparse.ArgumentParser(prog="beatcut", description="Cut a folder of clips to the beat of a track.")
    parser.add_argument("--clips", type=Path, required=True, help="folder of video clips")
    parser.add_argument("--track", type=Path, required=True, help="audio track")
    parser.add_argument("--out", type=Path, required=True, help="output video, e.g. final.mp4")
    parser.add_argument("--duration", type=_positive_float, default=30.0, help="output length in seconds (default 30)")
    parser.add_argument("--every", type=_positive_int, default=2, help="cut every N beats (default 2)")
    parser.add_argument("--aspect", choices=list(render.ASPECTS), default="9:16", help="output aspect (default 9:16)")
    parser.add_argument("--seed", type=int, default=None, help="seed for a reproducible edit")

    more = parser.add_argument_group("more options")
    more.add_argument("--start", type=_start, default=0.0, metavar="SECONDS|auto",
                      help="where in the track the edit begins; 'auto' picks the loudest stretch (default 0)")
    more.add_argument("--meter", type=int, choices=list(audio.METERS), default=4,
                      help="beats per bar: 4 for 4/4, 3 for 3/4, 6 for 6/8 (default 4)")
    more.add_argument("--pick", choices=list(plan.PICKS), default="motion",
                      help="'motion' takes the most frame-to-frame change; 'sharp' also avoids blur and camera whips")
    more.add_argument("--match-energy", action="store_true",
                      help="put the strongest footage on the loudest slots and calm footage on quiet ones")
    more.add_argument("--crop", choices=list(plan.CROPS), default="centre",
                      help="'centre' crop, or 'motion' to frame each shot on the action (default centre)")
    more.add_argument("--trim-edges", type=_seconds, default=0.0, metavar="SECONDS",
                      help="avoid this much of the start and end of every clip (default 0)")
    more.add_argument("--encoder", choices=list(render.ENCODERS), default="x264",
                      help="'hardware' encodes with the Mac's VideoToolbox, which is faster (default x264)")
    args = parser.parse_args(argv)

    if not args.clips.is_dir():
        parser.error(f"--clips: {args.clips} is not a folder")
    if not args.track.is_file():
        parser.error(f"--track: {args.track} not found")

    try:
        make_edit(
            args.clips, args.track, args.out, args.duration, args.every, args.aspect, args.seed,
            start=args.start, meter=args.meter, pick=args.pick, match_energy=args.match_energy,
            crop=args.crop, trim_edges=args.trim_edges, encoder=args.encoder, log=_stderr,
        )
    except (ValueError, ffutil.FFmpegError) as err:
        print(f"beatcut: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
