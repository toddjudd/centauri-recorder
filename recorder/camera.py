"""Single connection to the printer's MJPEG stream, fanned out to subscribers."""
import asyncio
import logging

import aiohttp

log = logging.getLogger("camera")


class Camera:
    def __init__(self, url: str):
        self.url = url
        self.latest: bytes | None = None
        self.latest_at = 0.0
        self._subscribers: set[asyncio.Queue[bytes]] = set()

    def subscribe(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=30)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        self._subscribers.discard(q)

    def _publish(self, frame: bytes) -> None:
        loop = asyncio.get_running_loop()
        self.latest, self.latest_at = frame, loop.time()
        for q in self._subscribers:
            if q.full():  # slow consumer: drop its oldest frame rather than block the stream
                q.get_nowait()
            q.put_nowait(frame)

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=15)
        while True:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as http:
                    async with http.get(self.url) as resp:
                        resp.raise_for_status()
                        log.info("camera stream connected")
                        await self._read(resp.content)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("camera stream error: %r; reconnecting in 5s", e)
            await asyncio.sleep(5)

    async def _read(self, stream: aiohttp.StreamReader) -> None:
        # Parts look like: --boundary\r\nContent-Type: image/jpeg\r\nContent-Length: N\r\n\r\n<jpeg>
        while True:
            line = await stream.readline()
            if not line:
                raise ConnectionError("stream ended")
            if not line.lower().startswith(b"content-length:"):
                continue
            length = int(line.split(b":", 1)[1])
            while (await stream.readline()) not in (b"\r\n", b"\n"):
                pass
            self._publish(await stream.readexactly(length))
