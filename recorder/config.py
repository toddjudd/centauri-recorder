import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    printer_host: str = _env("PRINTER_HOST", "192.168.4.33")
    data_dir: Path = Path(_env("DATA_DIR", "/data"))
    web_port: int = int(_env("WEB_PORT", "8080"))

    # Real-time video: keep the first N minutes and a rolling last N minutes.
    first_minutes: int = int(_env("FIRST_MINUTES", "45"))
    buffer_minutes: int = int(_env("BUFFER_MINUTES", "60"))
    segment_seconds: int = int(_env("SEGMENT_SECONDS", "300"))
    video_crf: int = int(_env("VIDEO_CRF", "30"))

    # Timelapse: a frame on every layer change, or every N seconds, whichever is first.
    timelapse_interval: int = int(_env("TIMELAPSE_INTERVAL", "60"))
    timelapse_fps: int = int(_env("TIMELAPSE_FPS", "30"))
    timelapse_rebuild_minutes: int = int(_env("TIMELAPSE_REBUILD_MINUTES", "60"))

    # How long the printer must report "not printing" before the session is closed.
    end_grace_seconds: int = int(_env("END_GRACE_SECONDS", "60"))

    # Discord bot that DMs DISCORD_USER_ID when a print ends.
    discord_bot_token: str = _env("DISCORD_BOT_TOKEN", "")
    discord_user_id: str = _env("DISCORD_USER_ID", "")
    # Address of this dashboard, linked in the notification.
    public_url: str = _env("PUBLIC_URL", "")

    @property
    def notifications_enabled(self) -> bool:
        return bool(self.discord_bot_token and self.discord_user_id)

    @property
    def camera_url(self) -> str:
        return f"http://{self.printer_host}:3031/video"

    @property
    def sdcp_url(self) -> str:
        return f"ws://{self.printer_host}:3030/websocket"
