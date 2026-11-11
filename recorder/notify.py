"""Print-ended notifications as a Discord DM from a bot (same setup as the homelab watchdog scripts)."""
import json
import logging

import aiohttp

from .config import Config
from .session import Session

log = logging.getLogger("notify")
API = "https://discord.com/api/v10"

# end_status -> (emoji, title)
OUTCOMES = {
    "complete": ("✅", "Print complete"),
    "stopped": ("🛑", "Print stopped"),
    "printer_offline": ("⚠️", "Printer went offline"),
    "superseded": ("ℹ️", "Print replaced by a new one"),
}


def _duration(seconds: float) -> str:
    m = int(seconds // 60)
    d, h, m = m // 1440, m % 1440 // 60, m % 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m" if h else f"{m}m"


def build(cfg: Config, session: Session) -> str:
    meta = session.meta
    status = meta["end_status"]
    emoji, title = OUTCOMES.get(status, ("⚠️", f"Print ended: {status.replace('_', ' ')}"))
    name = meta["filename"].removesuffix(".gcode").replace("_", " ").strip()
    duration = _duration(meta["ended_at"] - meta["started_at"])
    lines = [f"{emoji} **{title}**: {name}",
             f"{duration} · layer {meta['last_layer']}/{meta['total_layers']}"]
    if cfg.public_url:
        lines.append(cfg.public_url)
    return "\n".join(lines)


async def send(cfg: Config, content: str, image: bytes | None = None) -> None:
    headers = {"Authorization": f"Bot {cfg.discord_bot_token}"}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as http:
        async with http.post(f"{API}/users/@me/channels", json={"recipient_id": cfg.discord_user_id}) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Discord DM channel: {resp.status} {(await resp.text())[:200]}")
            channel = (await resp.json())["id"]
        form = aiohttp.FormData()
        payload: dict = {"content": content}
        if image:
            payload["attachments"] = [{"id": 0, "filename": "print.jpg"}]
            form.add_field("files[0]", image, filename="print.jpg", content_type="image/jpeg")
        form.add_field("payload_json", json.dumps(payload), content_type="application/json")
        async with http.post(f"{API}/channels/{channel}/messages", data=form) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Discord message: {resp.status} {(await resp.text())[:200]}")


async def notify_print_ended(cfg: Config, session: Session) -> None:
    if not cfg.notifications_enabled:
        return
    latest = session.dir / "latest.jpg"
    try:
        await send(cfg, build(cfg, session), latest.read_bytes() if latest.exists() else None)
        log.info("sent print-ended Discord DM")
    except Exception:
        log.exception("print-ended notification failed")
