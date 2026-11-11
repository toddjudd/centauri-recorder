"""One recording session per print job. Lives in DATA_DIR/current and is wiped when the next print starts."""
import asyncio
import io
import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .camera import Camera
from .config import Config
from .printer import PrinterState

log = logging.getLogger("session")

SEGMENT_PATTERN = "%Y%m%d-%H%M%S"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def _label(st: PrinterState) -> str:
    return (f"{datetime.now():%Y-%m-%d %H:%M:%S}  layer {st.layer}/{st.total_layers}  "
            f"{st.progress}%  {st.print_status_name}  nozzle {st.nozzle:.0f}C  bed {st.bed:.0f}C")


async def _ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
        stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace')[-2000:]}")


class Session:
    def __init__(self, cfg: Config, camera: Camera, meta: dict):
        self.cfg = cfg
        self.camera = camera
        self.meta = meta
        self.dir = cfg.data_dir / "current"
        self.segments = self.dir / "segments"
        self.frames = self.dir / "frames"
        self.overlay = self.dir / "overlay.txt"
        self._tasks: list[asyncio.Task] = []
        self._encoder: asyncio.subprocess.Process | None = None
        self._last_layer = -1
        self._last_snapshot = 0.0
        self._frame_count = len(list(self.frames.glob("*.jpg"))) if self.frames.exists() else 0

    @property
    def task_id(self) -> str:
        return self.meta["task_id"]

    # ---------- lifecycle ----------

    @classmethod
    def load_open(cls, cfg: Config, camera: Camera) -> "Session | None":
        path = cfg.data_dir / "current" / "session.json"
        if not path.exists():
            return None
        meta = json.loads(path.read_text())
        return None if meta.get("ended_at") else cls(cfg, camera, meta)

    @classmethod
    def create(cls, cfg: Config, camera: Camera, st: PrinterState) -> "Session":
        current = cfg.data_dir / "current"
        if current.exists():
            log.info("removing previous recording")
            shutil.rmtree(current)
        meta = {"task_id": st.task_id, "filename": st.filename, "started_at": time.time(),
                "total_layers": st.total_layers, "ended_at": None, "end_status": None,
                "last_layer": st.layer, "status_changes": []}
        return cls(cfg, camera, meta)

    def start(self, st: PrinterState) -> None:
        log.info("recording %s (task %s)", self.meta["filename"], self.task_id)
        self.segments.mkdir(parents=True, exist_ok=True)
        self.frames.mkdir(parents=True, exist_ok=True)
        self.update(st)
        self._tasks = [asyncio.create_task(t) for t in
                       (self._encode_realtime(), self._prune_loop(), self._timelapse_loop())]

    def update(self, st: PrinterState) -> None:
        self.overlay.write_text(_label(st))
        changes = self.meta["status_changes"]
        if not changes or changes[-1]["status"] != st.print_status_name:
            changes.append({"at": time.time(), "status": st.print_status_name, "layer": st.layer})
        self.meta["last_layer"] = st.layer
        self._save()
        now = time.monotonic()
        if st.layer != self._last_layer or now - self._last_snapshot >= self.cfg.timelapse_interval:
            self._last_layer, self._last_snapshot = st.layer, now
            self._snapshot(st)

    async def finish(self, end_status: str) -> None:
        log.info("print ended (%s); finalizing recording", end_status)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.meta["ended_at"] = time.time()
        self.meta["end_status"] = end_status
        try:
            await self._assemble_realtime()
            await self._build_timelapse()
            shutil.rmtree(self.frames, ignore_errors=True)
        except Exception:
            log.exception("finalizing failed; leaving raw segments/frames in place")
        self.overlay.unlink(missing_ok=True)
        self._save()
        log.info("recording finalized in %s", self.dir)

    def _save(self) -> None:
        tmp = self.dir / "session.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2))
        tmp.replace(self.dir / "session.json")

    # ---------- timelapse ----------

    def _snapshot(self, st: PrinterState) -> None:
        frame = self.camera.latest
        if not frame or asyncio.get_running_loop().time() - self.camera.latest_at > 10:
            return
        img = Image.open(io.BytesIO(frame)).convert("RGB")
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype(FONT, 14)
        text = _label(st)
        box = draw.textbbox((6, 6), text, font=font)
        draw.rectangle((box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill=(0, 0, 0))
        draw.text((6, 6), text, font=font, fill=(255, 255, 255))
        self._frame_count += 1
        img.save(self.frames / f"{self._frame_count:06d}.jpg", quality=85)
        img.save(self.dir / "latest.jpg", quality=85)

    async def _build_timelapse(self) -> None:
        if not any(self.frames.glob("*.jpg")):
            return
        tmp = self.dir / "timelapse.tmp.mp4"
        await _ffmpeg("-framerate", str(self.cfg.timelapse_fps), "-pattern_type", "glob",
                      "-i", str(self.frames / "*.jpg"), "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp))
        tmp.replace(self.dir / "timelapse.mp4")

    async def _timelapse_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.timelapse_rebuild_minutes * 60)
            try:
                await self._build_timelapse()
            except Exception:
                log.exception("interim timelapse build failed")

    # ---------- real-time video ----------

    async def _encode_realtime(self) -> None:
        queue = self.camera.subscribe()
        try:
            while True:
                await self._run_encoder(queue)
                log.warning("real-time encoder exited; restarting in 5s")
                await asyncio.sleep(5)
        finally:
            self.camera.unsubscribe(queue)
            await self._stop_encoder()

    async def _run_encoder(self, queue: asyncio.Queue[bytes]) -> None:
        drawtext = (f"drawtext=fontfile={FONT}:textfile={self.overlay}:reload=1:expansion=none:x=6:y=6:fontsize=14:"
                    "fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=4")
        self._encoder = proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-use_wallclock_as_timestamps", "1", "-f", "mjpeg", "-i", "pipe:0",
            "-vf", drawtext, "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(self.cfg.video_crf), "-pix_fmt", "yuv420p", "-g", "20", "-fps_mode", "vfr",
            # MPEG-TS segments stay readable if ffmpeg is killed mid-segment (MP4 would lose its index).
            "-f", "segment", "-segment_time", str(self.cfg.segment_seconds), "-segment_format", "mpegts",
            "-segment_format_options", "flush_packets=1",
            "-strftime", "1", str(self.segments / f"{SEGMENT_PATTERN}.ts"),
            stdin=asyncio.subprocess.PIPE)
        assert proc.stdin
        try:
            while proc.returncode is None:
                frame = await queue.get()
                proc.stdin.write(frame)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        await proc.wait()

    async def _stop_encoder(self) -> None:
        proc = self._encoder
        if proc and proc.returncode is None:
            proc.stdin.close()  # let ffmpeg flush the last segment
            try:
                await asyncio.wait_for(proc.wait(), 30)
            except asyncio.TimeoutError:
                proc.kill()

    def _segment_start(self, path: Path) -> float:
        return datetime.strptime(path.stem, SEGMENT_PATTERN).timestamp()

    def _split_segments(self, now: float) -> tuple[list[Path], list[Path]]:
        """Return (first-window segments, last-window segments) by segment start time."""
        segs = sorted(s for s in self.segments.glob("*.ts") if s.stat().st_size)
        first_end = self.meta["started_at"] + self.cfg.first_minutes * 60
        last_start = now - self.cfg.buffer_minutes * 60 - self.cfg.segment_seconds
        first = [s for s in segs if self._segment_start(s) < first_end]
        last = [s for s in segs if s not in first and self._segment_start(s) >= last_start]
        return first, last

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            first, last = self._split_segments(time.time())
            keep = set(first) | set(last)
            for seg in self.segments.glob("*.ts"):
                if seg not in keep:
                    seg.unlink()

    async def _assemble_realtime(self) -> None:
        first, last = self._split_segments(self.meta["ended_at"])
        everything = sorted(s for s in self.segments.glob("*.ts") if s.stat().st_size)
        if everything == first + last:  # no gap: the whole print fits in one file
            outputs = {"full.mp4": everything}
        else:
            outputs = {"first_layers.mp4": first, "final_minutes.mp4": last}
        for name, segs in outputs.items():
            if not segs:
                continue
            listing = self.segments / "concat.txt"
            listing.write_text("".join(f"file '{s.name}'\n" for s in segs))
            await _ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                          "-movflags", "+faststart", str(self.dir / name))
        shutil.rmtree(self.segments)
