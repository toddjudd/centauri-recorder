# centauri-recorder

Records each print on an Elegoo Centauri Carbon so you can diagnose failures afterwards. It keeps only the most recent print: starting a new print deletes the previous recording.

For each print it produces, in `data/current/`:

| File | What it is |
|---|---|
| `timelapse.mp4` | One frame per layer change (or every `TIMELAPSE_INTERVAL` s), rebuilt hourly during the print |
| `first_layers.mp4` | Real-time video of the first `FIRST_MINUTES` (bed adhesion) |
| `final_minutes.mp4` | Real-time video of the last `BUFFER_MINUTES` before the print ended or failed |
| `full.mp4` | Replaces the two above when the print was short enough to keep it all |
| `session.json` | File name, start/end time, end status, status changes with layer numbers |

Every frame has an overlay with time, layer, progress, printer status and temperatures.

Storage stays small regardless of print length: about 150 MB of real-time video plus the timelapse frames, which are deleted once the final timelapse is built.

## How it works

- Reads printer status from the SDCP websocket (`ws://PRINTER:3030/websocket`). A session starts when a print job appears and ends `END_GRACE_SECONDS` after the printer stops reporting it as printing.
- Opens one connection to the camera (`http://PRINTER:3031/video`, 640x360 MJPEG at ~10 fps) and fans frames out to the encoder, the timelapse and the web UI's live view.
- Real-time video is encoded in 5-minute segments. Segments outside the first and last windows are deleted as the print runs, then the kept segments are joined at the end.
- If the container restarts mid-print, it resumes the same session.

## Run on the NAS

```sh
docker compose up -d --build
```

Open `http://NAS:8080` for the dashboard: live camera, the recorded videos with download links, print progress, temperatures and a status timeline. It refreshes every 3 seconds. Configuration is in `docker-compose.yml`. Set `TZ` so the overlay timestamps are in local time.

## Run locally

Needs Python 3.12 and an ffmpeg build with `drawtext` (libfreetype).

```sh
pip install -r requirements.txt
DATA_DIR=./data PRINTER_HOST=192.168.4.33 python -m recorder.main
```

## Tests

The tests drive the real encoder with a fake printer, so they need the same ffmpeg (plus ffprobe) on `PATH`. They take about 2 minutes.

```sh
pip install -r requirements.txt pytest
python -m pytest tests
```
