import asyncio
import logging
import time

from aiohttp import web

from .camera import Camera
from .config import Config
from .printer import Printer
from .session import Session
from .web import make_app

log = logging.getLogger("main")

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

        # The printer clears TaskId when a print ends, so only a *different* active job supersedes.
        if session and st.printing and st.task_id and session.task_id != st.task_id:
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
