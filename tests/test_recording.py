"""End-to-end: fake printer states + a real JPEG fed through the real camera fan-out and ffmpeg."""
import asyncio
import json
import os
import signal
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from recorder.camera import Camera
from recorder.config import Config
from recorder.main import supervise
from recorder.printer import PrinterState

FRAME = (Path(__file__).parent / "frame.jpg").read_bytes()
PRINTING = PrinterState(printing=True, print_status=13, task_id="task-a", filename="a.gcode",
                        layer=1, total_layers=10, progress=0, nozzle=250, bed=70)
# What the Centauri Carbon reports right after a print completes: TaskId and Filename are cleared.
COMPLETE = replace(PRINTING, printing=False, print_status=9, task_id="", filename="", layer=10)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout)


def kill_segment_encoder() -> None:
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            args = (proc / "cmdline").read_bytes().split(b"\0")
            ppid = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        if ppid == os.getpid() and Path(args[0].decode()).name == "ffmpeg" and b"segment" in args:
            os.kill(int(proc.name), signal.SIGKILL)


async def record(tmp_path: Path, seconds: float, restart_at: float | None = None, **cfg) -> Path:
    config = Config(data_dir=tmp_path, segment_seconds=4, end_grace_seconds=2, timelapse_interval=1, **cfg)
    camera, printer = Camera(""), SimpleNamespace(state=PRINTING)

    async def feed():
        while True:
            camera._publish(FRAME)
            await asyncio.sleep(0.1)

    tasks = [asyncio.create_task(feed()), asyncio.create_task(supervise(config, camera, printer))]
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < seconds:
        if restart_at is not None and loop.time() - start >= restart_at:
            # Simulate a container crash: ffmpeg and every task except the frame feed dies, then supervise starts over.
            restart_at = None
            kill_segment_encoder()  # a hard kill leaves a truncated last segment
            for t in asyncio.all_tasks() - {asyncio.current_task(), tasks[0]}:
                t.cancel()
            await asyncio.sleep(1)
            tasks[1] = asyncio.create_task(supervise(config, camera, printer))
        printer.state = replace(PRINTING, layer=1 + int(loop.time() - start) // 3)
        await asyncio.sleep(0.5)
    printer.state = COMPLETE
    current = tmp_path / "current"
    while json.loads((current / "session.json").read_text())["ended_at"] is None:
        await asyncio.sleep(0.5)
    for t in tasks:
        t.cancel()
    return current


def test_completed_print_keeps_whole_recording(tmp_path):
    current = asyncio.run(record(tmp_path, 20))
    meta = json.loads((current / "session.json").read_text())
    assert meta["end_status"] == "complete"
    assert not (current / "segments").exists() and not (current / "frames").exists()
    # Spans several 4 s segments; must not be truncated to the first one.
    assert duration(current / "full.mp4") > 17
    assert duration(current / "timelapse.mp4") > 0


def test_restart_mid_print_resumes_same_recording(tmp_path):
    current = asyncio.run(record(tmp_path, 24, restart_at=10))
    assert json.loads((current / "session.json").read_text())["end_status"] == "complete"
    # Both encoder runs end up in one file. Expected loss: restart gap, startup polling, and the
    # unflushed tail of the killed encoder (at most one 2 s GOP). One segment alone is 4 s.
    assert duration(current / "full.mp4") > 14


def test_long_print_keeps_first_and_last_windows(tmp_path):
    current = asyncio.run(record(tmp_path, 40, first_minutes=0.1, buffer_minutes=0.1))
    assert not (current / "full.mp4").exists()
    assert 2 < duration(current / "first_layers.mp4") < 10
    assert 2 < duration(current / "final_minutes.mp4") < 15


def test_completed_print_sends_discord_dm_with_snapshot(tmp_path, monkeypatch):
    from aiohttp import web

    from recorder import notify

    received = []

    async def run():
        async def open_dm(request):
            received.append(("dm", request.headers["Authorization"], await request.json()))
            return web.json_response({"id": "chan1"})

        async def message(request):
            form = await request.post()
            received.append(("msg", json.loads(form["payload_json"]), form["files[0]"].file.read()))
            return web.json_response({"id": "m1"})

        app = web.Application()
        app.router.add_post("/users/@me/channels", open_dm)
        app.router.add_post("/channels/chan1/messages", message)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        monkeypatch.setattr(notify, "API", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}")
        try:
            await record(tmp_path, 6, discord_bot_token="tok", discord_user_id="42",
                         public_url="https://centauri.example")
            await asyncio.sleep(1)
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert received[0] == ("dm", "Bot tok", {"recipient_id": "42"})
    _, payload, image = received[1]
    assert payload["content"].startswith("✅ **Print complete**: a\n")
    assert payload["content"].endswith("https://centauri.example")
    assert payload["attachments"] == [{"id": 0, "filename": "print.jpg"}]
    assert image[:2] == b"\xff\xd8"
