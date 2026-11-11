import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import aiohttp
from aiohttp import web

from .camera import Camera
from .config import Config
from .notify import send
from .printer import Printer

STATIC = Path(__file__).parent / "static"
VIDEOS = ("timelapse.mp4", "first_layers.mp4", "final_minutes.mp4", "full.mp4")


def make_app(cfg: Config, camera: Camera, printer: Printer) -> web.Application:
    current = cfg.data_dir / "current"

    async def status(_: web.Request) -> web.Response:
        meta_path = current / "session.json"
        session = json.loads(meta_path.read_text()) if meta_path.exists() else None
        st = printer.state
        files = [{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
                 for p in sorted(current.glob("*")) if p.is_file() and p.name in (*VIDEOS, "latest.jpg")]
        usage = sum(p.stat().st_size for p in current.rglob("*") if p.is_file()) if current.exists() else 0
        frame_age = asyncio.get_running_loop().time() - camera.latest_at
        return web.json_response({
            "printer": {**asdict(st), "status_name": st.print_status_name} if st else None,
            "camera_ok": camera.latest is not None and frame_age < 10,
            "session": session,
            "files": files,
            "usage_bytes": usage,
            "notifications": cfg.notifications_enabled,
        })

    async def notify_test(_: web.Request) -> web.Response:
        if not cfg.notifications_enabled:
            raise web.HTTPNotFound()
        try:
            await send(cfg, "🔔 **Centauri Recorder** test. You'll get a DM like this when a print ends.", camera.latest)
        except (aiohttp.ClientError, RuntimeError, TimeoutError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=502)
        return web.json_response({"ok": True})

    async def live(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame",
                                           "Cache-Control": "no-store"})
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

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/notify/test", notify_test)
    app.router.add_get("/live", live)
    app.router.add_get("/files/{name}", files)
    app.router.add_static("/static", STATIC)
    return app
