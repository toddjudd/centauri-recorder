"""Print-ended notifications through ntfy (https://ntfy.sh or a self-hosted server)."""
import logging

import aiohttp

from .config import Config
from .session import Session

log = logging.getLogger("notify")

# end_status -> (title, ntfy emoji tag, priority 1-5)
OUTCOMES = {
    "complete": ("Print complete", "white_check_mark", 3),
    "stopped": ("Print stopped", "octagonal_sign", 4),
    "printer_offline": ("Printer went offline", "warning", 4),
    "superseded": ("Print replaced by a new one", "information_source", 3),
}


def _duration(seconds: float) -> str:
    m = int(seconds // 60)
    d, h, m = m // 1440, m % 1440 // 60, m % 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m" if h else f"{m}m"


def build(session: Session) -> dict[str, str]:
    meta = session.meta
    status = meta["end_status"]
    title, tag, priority = OUTCOMES.get(status, (f"Print ended: {status.replace('_', ' ')}", "warning", 4))
    name = meta["filename"].removesuffix(".gcode").replace("_", " ").strip()
    duration = _duration(meta["ended_at"] - meta["started_at"])
    return {
        "title": title,
        "message": f"{name}\n{duration} · layer {meta['last_layer']}/{meta['total_layers']}",
        "tags": tag,
        "priority": str(priority),
    }


async def send(cfg: Config, params: dict[str, str], image: bytes | None = None) -> None:
    if cfg.public_url:
        params = {**params, "click": cfg.public_url}
    if image:
        params = {**params, "filename": "print.jpg"}
    headers = {"Authorization": f"Bearer {cfg.ntfy_token}"} if cfg.ntfy_token else {}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as http, \
            http.put(cfg.ntfy_url, params=params, data=image or b"", headers=headers) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"ntfy returned {resp.status}: {(await resp.text())[:200]}")


async def notify_print_ended(cfg: Config, session: Session) -> None:
    if not cfg.ntfy_url:
        return
    latest = session.dir / "latest.jpg"
    try:
        await send(cfg, build(session), latest.read_bytes() if latest.exists() else None)
        log.info("sent print-ended notification")
    except Exception:
        log.exception("print-ended notification failed")
