"""Local web UI server. Binds to 127.0.0.1 only; nothing leaves the machine."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import unquote

import numpy as np
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import audio, ffutil, motion, plan, render

FPS = 30
PEAK_RATE = 105  # waveform peaks per second; divides the 22050 Hz sample rate exactly
STATIC = Path(__file__).parent / "static"
PREVIEW_SIZES = {"9:16": (360, 640), "1:1": (480, 480), "16:9": (640, 360)}
FOLDERS = ("clips", "track", "thumbs", "out", "cache")


class Workspace:
    """The project folder: uploaded media, cached analysis and saved settings.

    It persists between runs, so reopening the UI brings the project back.
    """

    def __init__(self, root: Path):
        self.root = root
        self.render_lock = threading.Lock()
        self.progress = {"active": False, "done": 0, "total": 0}
        self._open()

    def _open(self) -> None:
        for name in FOLDERS:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.clips_dir, self.track_dir, self.thumbs, self.out, self.cache = (self.root / n for n in FOLDERS)
        self.clips: dict[str, motion.Clip] = {}
        self.track: Path | None = None
        self.analyses: dict[int, audio.Analysis] = {}
        self.peaks: list[int] = []
        for path in sorted(self.clips_dir.iterdir()):
            if path.suffix.lower() in motion.VIDEO_EXTS:
                try:
                    self.add_clip(path)
                except ffutil.FFmpegError:
                    pass
        tracks = [p for p in self.track_dir.iterdir() if not p.name.startswith(".")]
        if tracks:
            try:
                self.set_track(tracks[0])
            except (ValueError, ffutil.FFmpegError):
                pass

    def reset(self) -> None:
        for name in FOLDERS:
            shutil.rmtree(self.root / name, ignore_errors=True)
        self.project_file.unlink(missing_ok=True)
        self._open()

    @property
    def project_file(self) -> Path:
        return self.root / "project.json"

    def _cache_path(self, name: str) -> Path:
        return self.cache / (hashlib.md5(name.encode()).hexdigest()[:16] + ".npz")

    def add_clip(self, path: Path) -> bool:
        clip = motion.load_clip(path, self._cache_path(path.name))
        if clip is None:
            clip = motion.analyse_clip(path)
            if clip.duration > 0:
                motion.save_clip(clip, self._cache_path(path.name))
        if clip.duration <= 0:
            return False
        self.clips[path.name] = clip
        return True

    def remove_clip(self, name: str) -> None:
        if self.clips.pop(name, None) is not None:
            (self.clips_dir / name).unlink(missing_ok=True)
            self._cache_path(name).unlink(missing_ok=True)

    def set_track(self, path: Path) -> None:
        analysis = audio.analyse(path)
        self.peaks = _peaks(path)
        self.track = path
        self.analyses = {analysis.meter: analysis}

    def analysis(self, meter: int = 4) -> audio.Analysis:
        if meter not in audio.METERS:
            meter = 4
        if meter not in self.analyses:
            self.analyses[meter] = audio.analyse(self.track, meter)
        return self.analyses[meter]

    def ordered_clips(self) -> list[motion.Clip]:
        return [self.clips[name] for name in sorted(self.clips)]

    def state(self) -> dict:
        project = None
        try:
            project = json.loads(self.project_file.read_text())
        except (OSError, ValueError):
            pass
        return {
            "clips": [{"name": c.path.name, "duration": round(c.duration, 2)} for c in self.ordered_clips()],
            "track": self.track.name if self.track else None,
            "project": project,
            "hardware": render.hardware_available(),
        }


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _safe_name(raw: str) -> str | None:
    name = Path(unquote(raw)).name
    return None if not name or name.startswith(".") else name


async def _save_body(request: Request, dest: Path) -> None:
    with dest.open("wb") as fh:
        async for chunk in request.stream():
            fh.write(chunk)


def _peaks(path: Path) -> list[int]:
    y = np.abs(audio.load(path))
    step = audio.SR // PEAK_RATE
    y = y[: len(y) // step * step].reshape(-1, step).max(axis=1)
    top = float(y.max()) or 1.0
    return np.round(y / top * 255).astype(int).tolist()


def _options(body: dict) -> plan.Options:
    pick = body.get("pick", "motion")
    crop = body.get("crop", "centre")
    return plan.Options(
        seed=int(body.get("seed", 0)),
        pick=pick if pick in plan.PICKS else "motion",
        match_energy=bool(body.get("match_energy", False)),
        crop=crop if crop in plan.CROPS else "centre",
        trim_edges=max(0.0, float(body.get("trim_edges", 0))),
    )


def _locks(body: dict) -> list[plan.Lock]:
    out = []
    for item in body.get("locks") or []:
        try:
            out.append(plan.Lock(float(item["start"]), str(item["clip"]), float(item["clip_start"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def create_app(root: Path) -> Starlette:
    ws = Workspace(root)

    async def get_state(request: Request):
        return JSONResponse(ws.state())

    async def put_clip(request: Request):
        name = _safe_name(request.path_params["name"])
        if name is None or Path(name).suffix.lower() not in motion.VIDEO_EXTS:
            return _error("That file type is not supported as a clip.")
        dest = ws.clips_dir / name
        ws.remove_clip(name)
        await _save_body(request, dest)
        try:
            ok = await run_in_threadpool(ws.add_clip, dest)
        except ffutil.FFmpegError:
            ok = False
        if not ok:
            dest.unlink(missing_ok=True)
            return _error(f"Could not read video from {name}.")
        return JSONResponse(ws.state())

    async def delete_clip(request: Request):
        name = _safe_name(request.path_params["name"])
        if name:
            ws.remove_clip(name)
        return JSONResponse(ws.state())

    async def put_track(request: Request):
        name = _safe_name(request.path_params["name"])
        if name is None:
            return _error("Bad file name.")
        staging = ws.root / f"incoming-{secrets.token_hex(4)}"
        await _save_body(request, staging)
        try:
            await run_in_threadpool(audio.analyse, staging)  # reject unreadable files before replacing
        except (ValueError, ffutil.FFmpegError) as err:
            staging.unlink(missing_ok=True)
            return _error(str(err) if isinstance(err, ValueError) else f"Could not read audio from {name}.")
        shutil.rmtree(ws.track_dir)
        ws.track_dir.mkdir()
        staging.rename(ws.track_dir / name)
        await run_in_threadpool(ws.set_track, ws.track_dir / name)
        return JSONResponse(ws.state())

    async def get_track(request: Request):
        if ws.track is None:
            return _error("No track loaded.", 404)
        return FileResponse(ws.track)

    async def get_analysis(request: Request):
        if ws.track is None:
            return _error("No track loaded.", 404)
        meter = int(request.query_params.get("meter", 4))
        analysis = await run_in_threadpool(ws.analysis, meter)
        return JSONResponse({**analysis.to_json(), "peaks": ws.peaks, "peak_rate": PEAK_RATE})

    async def put_project(request: Request):
        """Save the UI's settings, cuts and locks so the project reopens as it was left."""
        body = await request.body()
        if len(body) > 1_000_000:
            return _error("Project is too large to save.")
        try:
            json.loads(body)
        except ValueError:
            return _error("Project is not valid JSON.")
        ws.project_file.write_bytes(body)
        return JSONResponse({"saved": True})

    async def post_reset(request: Request):
        if ws.render_lock.locked():
            return _error("A render is running.", 409)
        await run_in_threadpool(ws.reset)
        return JSONResponse(ws.state())

    def _timing(body: dict):
        analysis = ws.analysis(int(body.get("meter", 4)))
        start, duration = plan.window(analysis, body.get("start", 0), body.get("duration", 30), FPS)
        return analysis, start, duration

    async def post_cuts(request: Request):
        """Default cut grid for a start point, duration and beat interval."""
        if ws.track is None:
            return _error("Add a track first.")
        body = await request.json()
        analysis, start, duration = await run_in_threadpool(_timing, body)
        every = max(1, int(body.get("every", 2)))
        return JSONResponse({"start": start, "duration": duration,
                             "cuts": plan.cut_times(analysis, every, duration, FPS, start)})

    def _plan(body: dict, slot: int | None = None):
        analysis, start, duration = _timing(body)
        cuts = plan.clean_cuts(body.get("cuts", []), duration, FPS)
        energy = plan.slot_energy(analysis, [0.0, *cuts, duration], start)
        args = (cuts, duration, ws.ordered_clips(), _options(body), energy, _locks(body))
        if slot is not None:
            return start, duration, cuts, plan.alternatives(slot, *args)
        return start, duration, cuts, plan.build(*args)

    async def post_plan(request: Request):
        """Which clip range fills each slot, without rendering anything."""
        if ws.track is None or not ws.clips:
            return _error("Add clips and a track first.")
        try:
            start, duration, cuts, segments = await run_in_threadpool(_plan, await request.json())
        except plan.NotEnoughFootage as err:
            return _error(str(err))
        return JSONResponse({"start": start, "duration": duration, "cuts": cuts,
                             "segments": [s.to_json() for s in segments]})

    async def post_alternatives(request: Request):
        """Other footage that could fill one slot, strongest first."""
        if ws.track is None or not ws.clips:
            return _error("Add clips and a track first.")
        body = await request.json()
        try:
            *_, options = await run_in_threadpool(_plan, body, int(body.get("slot", -1)))
        except plan.NotEnoughFootage as err:
            return _error(str(err))
        return JSONResponse({"alternatives": [s.to_json() for s in options]})

    def _render(body: dict) -> dict:
        start, duration, cuts, segments = _plan(body)
        aspect = body.get("aspect", "9:16")
        if aspect not in render.ASPECTS:
            raise ValueError("Unknown aspect.")
        preview = body.get("mode") != "export"
        name = "preview.mp4" if preview else f"export-{time.strftime('%H%M%S')}.mp4"
        ws.progress.update(active=True, done=0, total=len(segments))

        def tick():
            ws.progress["done"] += 1

        try:
            encoder = render.render(
                segments, ws.track, ws.out / name,
                PREVIEW_SIZES[aspect] if preview else render.ASPECTS[aspect],
                FPS, duration, progress=tick, start=start,
                encoder="hardware" if body.get("fast") else "x264", draft=preview,
            )
        finally:
            ws.progress["active"] = False
        return {"url": f"/api/out/{name}?v={secrets.token_hex(4)}", "cuts": cuts, "encoder": encoder,
                "segments": [s.to_json() for s in segments]}

    async def post_render(request: Request):
        if ws.track is None or not ws.clips:
            return _error("Add clips and a track first.")
        body = await request.json()
        if not ws.render_lock.acquire(blocking=False):
            return _error("A render is already running.", 409)
        try:
            return JSONResponse(await run_in_threadpool(_render, body))
        except (ValueError, ffutil.FFmpegError) as err:
            return _error(str(err))
        finally:
            ws.render_lock.release()

    async def get_progress(request: Request):
        return JSONResponse(ws.progress)

    async def get_out(request: Request):
        name = _safe_name(request.path_params["name"])
        path = ws.out / name if name else None
        if path is None or not path.is_file():
            return _error("Not found.", 404)
        download = "beatcut.mp4" if "download" in request.query_params else None
        return FileResponse(path, filename=download)

    def _thumb(name: str, t: float) -> Path | None:
        clip = ws.clips.get(name)
        if clip is None:
            return None
        t = max(0.0, min(t, clip.duration - 0.1))
        key = hashlib.md5(name.encode()).hexdigest()[:12]
        dest = ws.thumbs / f"{key}_{int(t * 10)}.jpg"
        if not dest.exists():
            ffutil.ffmpeg(["-ss", f"{t:.2f}", "-i", str(clip.path), "-frames:v", "1",
                           "-vf", "scale=160:-2", "-q:v", "6", str(dest)])
        return dest

    async def get_thumb(request: Request):
        name = _safe_name(request.path_params["name"])
        try:
            t = float(request.query_params.get("t", 0))
            path = await run_in_threadpool(_thumb, name, t) if name else None
        except (ValueError, ffutil.FFmpegError):
            path = None
        if path is None:
            return _error("Not found.", 404)
        return FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    async def index(request: Request):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    routes = [
        Route("/", index),
        Route("/api/state", get_state),
        Route("/api/clips/{name}", put_clip, methods=["PUT"]),
        Route("/api/clips/{name}", delete_clip, methods=["DELETE"]),
        Route("/api/track/{name}", put_track, methods=["PUT"]),
        Route("/api/track", get_track),
        Route("/api/analysis", get_analysis),
        Route("/api/project", put_project, methods=["PUT"]),
        Route("/api/reset", post_reset, methods=["POST"]),
        Route("/api/cuts", post_cuts, methods=["POST"]),
        Route("/api/plan", post_plan, methods=["POST"]),
        Route("/api/alternatives", post_alternatives, methods=["POST"]),
        Route("/api/render", post_render, methods=["POST"]),
        Route("/api/progress", get_progress),
        Route("/api/out/{name}", get_out),
        Route("/api/thumb/{name}", get_thumb),
        Mount("/static", StaticFiles(directory=STATIC)),
    ]
    # Refuse requests addressed to any other host name (guards against DNS rebinding).
    middleware = [Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])]
    return Starlette(routes=routes, middleware=middleware)


def serve(port: int, project: Path, open_browser: bool = True) -> None:
    import uvicorn

    ffutil.require()
    url = f"http://127.0.0.1:{port}"
    print(f"beatcut UI running at {url}  (Ctrl+C to stop)")
    print(f"Project folder: {project}")
    app = create_app(project)
    if open_browser:
        import webbrowser

        threading.Timer(0.8, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
