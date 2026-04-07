"""SseBroadcaster — fan-out SSE event queue for connected web clients."""

from __future__ import annotations

import asyncio
import json
import uuid


class SseBroadcaster:
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def connect(self) -> tuple[str, asyncio.Queue]:
        cid = str(uuid.uuid4())
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._queues[cid] = q
        return cid, q

    async def disconnect(self, cid: str) -> None:
        async with self._lock:
            q = self._queues.pop(cid, None)
        if q is not None:
            await q.put(None)  # sentinel

    async def broadcast(self, event: dict) -> None:
        data = json.dumps(event)
        async with self._lock:
            queues = list(self._queues.values())
        for q in queues:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass
