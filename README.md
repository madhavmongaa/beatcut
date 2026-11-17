# beatcut

Project page: https://madhavmongaa.github.io/beatcut/

Takes a folder of video clips and one audio track, and outputs a single video cut on the beat.
Everything runs locally: librosa finds the beats, ffmpeg does the video work.

## Install

Requires Python 3.12 and ffmpeg.

```bash
brew install ffmpeg
```

```bash
python3.12 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -e .
```

## Usage

```bash
beatcut --clips ./clips --track song.mp3 --out final.mp4
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--clips` | required | Folder of clips (`.mp4 .mov .m4v .mkv .webm .avi`) |
| `--track` | required | Audio track; becomes the only audio in the output |
| `--out` | required | Output file |
| `--duration` | `30` | Output length in seconds |
| `--every` | `2` | Cut every N beats |
| `--aspect` | `9:16` | `9:16` (1080x1920), `1:1` (1080x1080) or `16:9` (1920x1080) |
| `--seed` | random | Seed for a reproducible edit; the seed used is printed and saved |

More options, all off by default:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--start` | `0` | Where in the track the edit begins, in seconds. `auto` picks the loudest stretch, starting on a downbeat |
| `--pick` | `motion` | `motion` takes the most frame-to-frame change. `sharp` also marks down blurred frames and camera whips, so action in a steady shot wins |
| `--match-energy` | off | Put the strongest footage on the loudest slots and calm footage on quiet ones, instead of filling slots in order |
| `--crop` | `centre` | `motion` frames each shot on where the action is instead of the centre |
| `--trim-edges` | `0` | Seconds to avoid at the start and end of every clip, where phone footage wobbles |
| `--meter` | `4` | Beats per bar: `4` for 4/4, `3` for 3/4, `6` for 6/8. Sets where downbeats fall |
| `--encoder` | `x264` | `hardware` encodes with the Mac's VideoToolbox. Falls back to x264 where that is missing |

### Examples

A 30-second vertical edit with the defaults:

```bash
beatcut --clips ./clips --track song.mp3 --out final.mp4
```

A faster 15-second square edit, cutting on every beat:

```bash
beatcut --clips ./clips --track song.mp3 --out square.mp4 --aspect 1:1 --every 1 --duration 15
```

A slower one-minute landscape edit that renders the same way every time:

```bash
beatcut --clips ./clips --track song.mp3 --out wide.mp4 --aspect 16:9 --every 4 --duration 60 --seed 42
```

An edit of the loudest 30 seconds, favouring sharp footage framed on the action:

```bash
beatcut --clips ./clips --track song.mp3 --out best.mp4 --start auto --pick sharp --crop motion --match-energy --trim-edges 0.5
```

## Web UI

```bash
beatcut ui
```

This opens a local page at `http://127.0.0.1:8765` (change it with `--port`). The server only
listens on your own machine.

The project (uploaded clips, the track, your cuts and settings) is kept in `~/.beatcut/project`
and comes back when you start the UI again. Use `--project DIR` for a different folder, and
**New project** in the UI to clear it. The folder holds copies of your clips, so it can get large.

- Drop clips and one audio file anywhere in the window.
- The thin strip above the timeline is the whole track. Drag its window to choose which part to
  use (it snaps to downbeats), or click **Jump to loudest part**. Moving it resets the cuts.
- The timeline shows the waveform, a tick for every beat, and an orange marker for every cut.
  Brighter waveform means a high-energy section, dimmer means a quiet one.
- Drag a marker to move a cut; it snaps to beats unless you hold Shift. Double-click to add or
  remove a marker.
- The strip underneath shows which clip fills each shot. Click a shot to see other footage that
  could go there and pick one, or lock the shot as it is. Locked shots carry an orange top edge
  and stay put when you press **Shuffle** or change other settings.
- The settings on the right are the same options as the command line: length, beats per cut,
  meter, shape, pick by motion or sharpness, centre or follow crop, match shots to energy,
  skip clip edges, and fast (hardware) export.
- **Preview** renders a quick low-resolution draft. **Export** renders at full resolution and
  downloads `beatcut.mp4`.
- Space plays and pauses. Click the timeline to move the playhead.

## How it works

1. **Beats.** librosa tracks tempo and beats. Each beat is then nudged onto the nearest sharp
   transient, and the grid is extended to both ends of the track. Downbeats are estimated by
   picking the beat of the bar that hits hardest (4 beats to a bar unless `--meter` says otherwise).
2. **Pacing.** Each bar's onset strength (on a linear scale, so loudness counts) is compared
   with the track's median. Normal bars cut
   every `--every` beats, high-energy bars twice as often, quiet bars half as often.
3. **Footage.** Each clip gets a motion curve (mean frame-to-frame change at 10 samples per
   second). Every slot is filled with the highest-motion window that has not been used yet.
   Picks are spread across clips, and one clip is never used twice in a row when there is a choice.
   With `--pick sharp` the motion score is multiplied down where the frame loses detail (blur)
   or more than half the picture changes at once (the camera moving, not the subject).
4. **Render.** Each segment is scaled, centre-cropped and encoded at 30 fps with its audio
   dropped, then the segments are joined and the track is laid underneath. HDR phone footage
   is converted to standard colour, and clips are decoded with VideoToolbox on a Mac. Cuts are snapped to
   frame boundaries, so they sit within 17 ms of the detected beat.

Alongside the video, beatcut writes `<out>.cuts.json` with the tempo, beats, cut times and the
clip range used for every segment.

If the clips cannot fill the duration without reusing footage, beatcut stops with an error
instead of repeating a segment.

## Tests

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

The test generates a 120 BPM click track and colour-bar clips with ffmpeg, runs the tool, and
asserts that both the planned cuts and the cuts found in the rendered file land within 40 ms
of the beats. A second file drives the web UI's API on the same media, and a third covers the options
(start point, meter, sharp picking, energy matching, motion crop, locks, hardware encoder).

## Limits

- Downbeats are a guess from which beat of the bar is loudest; the meter itself is not detected,
  so set `--meter` for songs that are not in 4/4.
- `--pick sharp` and `--crop motion` are judged on small greyscale frames, so they follow
  movement and blur, not faces or subjects.
- A crop is fixed for the length of a shot; it does not pan.
