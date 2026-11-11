import asyncio
import json
import logging
import time

from aiohttp import web

from .camera import Camera
from .config import Config
from .printer import Printer
from .session import Session

log = logging.getLogger("main")

VIDEOS = ("timelapse.mp4", "first_layers.mp4", "final_minutes.mp4", "full.mp4")


async def supervise(cfg: Config, camera: Camera, printer: Printer) -> None:
    session = Session.load_open(cfg, camera)
    running = False
    idle_since: float | None = None
    offline_since: float | None = None

    while True:
        await asyncio.sleep(2)
        st = printer.state

        if st is None:
            offline_since = offline_since or time.time()
            # A long outage while a print was recording usually means the printer was powered off.
            if session and running and time.time() - offline_since > cfg.end_grace_seconds * 10:
                await session.finish("printer_offline")
                session, running = None, False
            continue
        offline_since = None

        if session and session.task_id != st.task_id and (running or st.printing):
            await session.finish("superseded" if running else "unknown")
            session, running = None, False

        if session and not running:
            if st.printing and st.task_id == session.task_id:
                session.start(st)  # container restarted mid-print: resume
                running = True
            else:  # left open by a previous container that missed the end
                await session.finish(st.print_status_name)
                session = None
            continue

        if not session:
            if st.printing and st.task_id:
                session = Session.create(cfg, camera, st)
                session.start(st)
                running, idle_since = True, None
            continue

        if st.printing:
            idle_since = None
            session.update(st)
        else:
            idle_since = idle_since or time.time()
            if time.time() - idle_since >= cfg.end_grace_seconds:
                await session.finish(st.print_status_name)
                session, running = None, False


def make_app(cfg: Config, camera: Camera, printer: Printer) -> web.Application:
    current = cfg.data_dir / "current"

    async def index(_: web.Request) -> web.Response:
        meta = {}
        if (current / "session.json").exists():
            meta = json.loads((current / "session.json").read_text())
        st = printer.state
        videos = "".join(
            f'<h3>{v}</h3><video controls preload="metadata" src="/files/{v}"></video>'
            for v in VIDEOS if (current / v).exists())
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta["started_at"])) if meta else "-"
        live = (f"{st.print_status_name}, layer {st.layer}/{st.total_layers}, {st.progress}%"
                if st else "printer offline")
        body = f"""<!doctype html><html><head><meta charset="utf-8"><title>Centauri recorder</title>
<meta name="viewport" content="width=device-width">
<style>body{{font-family:sans-serif;max-width:960px;margin:1em auto;padding:0 1em;background:#111;color:#ddd}}
video,img{{width:100%;background:#000}} a{{color:#8cf}} td{{padding:2px 10px 2px 0}}</style></head><body>
<h1>Centauri recorder</h1>
<table><tr><td>Printer</td><td>{live}</td></tr>
<tr><td>Recording</td><td>{meta.get("filename", "none")}</td></tr>
<tr><td>Started</td><td>{started}</td></tr>
<tr><td>Ended</td><td>{meta.get("end_status") or ("in progress" if meta else "-")}</td></tr></table>
<p><a href="/live">Live view</a> &middot; <a href="/files/session.json">session.json</a></p>
{'<h3>Latest timelapse frame</h3><img src="/files/latest.jpg">' if (current / "latest.jpg").exists() else ""}
{videos or "<p>No videos yet. The timelapse is rebuilt periodically during a print.</p>"}
</body></html>"""
        return web.Response(text=body, content_type="text/html")

    async def live(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"})
        await resp.prepare(request)
        queue = camera.subscribe()
        try:
            while True:
                frame = await queue.get()
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        finally:
            camera.unsubscribe(queue)

    async def files(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        path = current / name
        if "/" in name or name.startswith(".") or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/live", live)
    app.router.add_get("/files/{name}", files)
    return app


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    camera, printer = Camera(cfg.camera_url), Printer(cfg.sdcp_url)

    runner = web.AppRunner(make_app(cfg, camera, printer))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.web_port).start()
    log.info("web UI on :%d, printer %s, data in %s", cfg.web_port, cfg.printer_host, cfg.data_dir)

    await asyncio.gather(camera.run(), printer.run(), supervise(cfg, camera, printer))


if __name__ == "__main__":
    asyncio.run(main())
