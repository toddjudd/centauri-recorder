"""SDCP websocket client for the Elegoo Centauri Carbon (port 3030)."""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("printer")

# PrintInfo.Status values observed/documented for the Centauri Carbon. Only used for labels.
PRINT_STATUS = {
    0: "idle", 1: "homing", 5: "pausing", 6: "paused", 7: "stopping", 8: "stopped",
    9: "complete", 10: "file_checking", 13: "printing", 15: "resuming", 16: "printing_recovery",
    20: "preparing", 21: "leveling",
}


@dataclass(frozen=True)
class PrinterState:
    printing: bool  # machine-level status says a print job is running (CurrentStatus contains 1)
    print_status: int
    task_id: str
    filename: str
    layer: int
    total_layers: int
    progress: int
    nozzle: float
    bed: float
    light: bool

    @property
    def print_status_name(self) -> str:
        return PRINT_STATUS.get(self.print_status, f"status_{self.print_status}")

    @classmethod
    def parse(cls, status: dict) -> "PrinterState":
        info = status.get("PrintInfo", {})
        return cls(
            printing=1 in status.get("CurrentStatus", []),
            print_status=int(info.get("Status", 0)),
            task_id=info.get("TaskId", ""),
            filename=info.get("Filename", ""),
            layer=int(info.get("CurrentLayer", 0)),
            total_layers=int(info.get("TotalLayer", 0)),
            progress=int(info.get("Progress", 0)),
            nozzle=float(status.get("TempOfNozzle", 0)),
            bed=float(status.get("TempOfHotbed", 0)),
            light=bool(status.get("LightStatus", {}).get("SecondLight", 0)),
        )


class Printer:
    def __init__(self, url: str):
        self.url = url
        self.state: PrinterState | None = None
        self.updated = asyncio.Event()
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    def _request(self, cmd: int, data: dict | None = None) -> str:
        return json.dumps({
            "Id": "",
            "Topic": "sdcp/request/",
            "Data": {"Cmd": cmd, "Data": data or {}, "RequestID": uuid.uuid4().hex,
                     "MainboardID": "", "TimeStamp": int(time.time()), "From": 1},
        })

    async def run(self) -> None:
        while True:
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(self.url, heartbeat=None) as ws:
                        log.info("printer websocket connected")
                        self._ws = ws
                        poll = asyncio.create_task(self._poll(ws))
                        try:
                            await self._read(ws)
                        finally:
                            poll.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("printer websocket error: %r; reconnecting in 5s", e)
            self.state, self._ws = None, None
            await asyncio.sleep(5)

    async def set_light(self, on: bool) -> None:
        """Cmd 403 switches the chamber light (SecondLight); RgbLight is unused on the Centauri Carbon."""
        if self._ws is None:
            raise ConnectionError("printer not connected")
        await self._ws.send_str(self._request(403, {"LightStatus": {"SecondLight": int(on), "RgbLight": [0, 0, 0]}}))

    async def _poll(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # Cmd 0 asks for a status refresh; it also keeps the printer pushing updates.
        while True:
            await ws.send_str(self._request(0))
            await asyncio.sleep(10)

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=60)
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise ConnectionError(f"websocket closed: {msg.type}")
            data = json.loads(msg.data)
            if "Status" in data:
                self.state = PrinterState.parse(data["Status"])
                self.updated.set()
