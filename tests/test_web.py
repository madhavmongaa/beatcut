"""The web UI's API, driven end to end on the synthetic media."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from beatcut import web

from conftest import BEAT
from test_synthetic import TOLERANCE, off_beat, probe, rendered_cuts


@pytest.fixture(scope="module")
def client(click_track, clips_dir, tmp_path_factory):
    client = TestClient(web.create_app(tmp_path_factory.mktemp("ws")))
    for clip in sorted(clips_dir.iterdir()):
        assert client.put(f"/api/clips/{clip.name}", content=clip.read_bytes()).status_code == 200
    assert client.put(f"/api/track/{click_track.name}", content=click_track.read_bytes()).status_code == 200
    return client


def test_state_and_analysis(client):
    state = client.get("/api/state").json()
    assert len(state["clips"]) == 8 and state["track"] == "click.wav"
    analysis = client.get("/api/analysis").json()
    assert analysis["tempo"] == pytest.approx(120, abs=2)
    assert len(analysis["peaks"]) == pytest.approx(40 * analysis["peak_rate"], abs=2)


def test_default_cuts_on_beats(client):
    res = client.post("/api/cuts", json={"duration": 30, "every": 2}).json()
    assert len(res["cuts"]) == 29
    assert off_beat(res["cuts"]).max() <= TOLERANCE


def test_moved_marker_is_rendered(client):
    # Move one cut off the grid by a beat and a half; the render must follow it.
    cuts = [2.0, 4.75, 6.0]
    body = {"cuts": cuts, "duration": 8, "seed": 5, "aspect": "9:16"}
    planned = client.post("/api/plan", json=body).json()
    assert [s["start"] for s in planned["segments"]] == pytest.approx([0, 2.0, 4.75, 6.0], abs=1 / 30)

    res = client.post("/api/render", json={**body, "mode": "preview"})
    assert res.status_code == 200
    name = res.json()["url"].split("/")[-1].split("?")[0]
    out = client.get(f"/api/out/{name}")
    assert out.status_code == 200

    # Same seed and cuts: the render uses exactly the segments the plan promised.
    assert res.json()["segments"] == planned["segments"]


def test_rejects_bad_uploads(client):
    assert client.put("/api/clips/notes.txt", content=b"hello").status_code == 400
    assert client.put("/api/clips/broken.mp4", content=b"not a video").status_code == 400
    assert len(client.get("/api/state").json()["clips"]) == 8


def test_not_enough_footage_is_reported(client):
    # 8 clips of 10 s cannot fill 40 s cut into 20 s slots.
    res = client.post("/api/plan", json={"cuts": [20.0], "duration": 40, "seed": 1})
    assert res.status_code == 400 and "footage" in res.json()["error"]


def test_swap_and_lock_one_shot(client):
    body = {"cuts": [2.0, 4.0, 6.0], "duration": 8, "seed": 5}
    before = client.post("/api/plan", json=body).json()["segments"]
    options = client.post("/api/alternatives", json={**body, "slot": 1}).json()["alternatives"]
    assert len(options) >= 2
    pick = next(o for o in options if o["clip"] != before[1]["clip"])

    lock = {"start": 2.0, "clip": pick["clip"], "clip_start": pick["clip_start"]}
    for seed in (5, 6, 7):  # shuffling must leave the locked shot alone
        after = client.post("/api/plan", json={**body, "seed": seed, "locks": [lock]}).json()["segments"]
        assert (after[1]["clip"], after[1]["clip_start"], after[1]["locked"]) == (pick["clip"], pick["clip_start"], True)
        assert not any(s["locked"] for i, s in enumerate(after) if i != 1)


def test_start_and_options_reach_the_planner(client):
    cuts = client.post("/api/cuts", json={"duration": 8, "every": 2, "start": 4.125}).json()
    assert cuts["start"] == pytest.approx(4.125)
    assert off_beat([c + cuts["start"] for c in cuts["cuts"]]).max() <= TOLERANCE
    body = {"cuts": cuts["cuts"], "duration": 8, "start": cuts["start"], "seed": 2,
            "pick": "sharp", "crop": "motion", "match_energy": True, "trim_edges": 0.5}
    segments = client.post("/api/plan", json=body).json()["segments"]
    assert all(s["clip_start"] >= 0.5 - 1e-6 for s in segments)
    assert client.post("/api/cuts", json={"duration": 8, "start": "auto"}).status_code == 200


def test_project_survives_a_restart(client, click_track, clips_dir, tmp_path):
    root = tmp_path / "project"
    first = TestClient(web.create_app(root))
    clip = sorted(clips_dir.iterdir())[0]
    first.put(f"/api/clips/{clip.name}", content=clip.read_bytes())
    first.put(f"/api/track/{click_track.name}", content=click_track.read_bytes())
    saved = {"wanted": 12, "cuts": [1.0, 2.5], "edited": True, "locks": []}
    assert first.put("/api/project", json=saved).status_code == 200

    again = TestClient(web.create_app(root)).get("/api/state").json()  # a fresh server on the same folder
    assert [c["name"] for c in again["clips"]] == [clip.name]
    assert again["track"] == click_track.name
    assert again["project"] == saved

    cleared = TestClient(web.create_app(root)).post("/api/reset").json()
    assert cleared["clips"] == [] and cleared["track"] is None and cleared["project"] is None
