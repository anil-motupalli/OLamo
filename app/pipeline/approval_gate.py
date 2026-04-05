"""ApprovalGate — async gate that suspends the pipeline until a human approves."""
from __future__ import annotations
import asyncio


class ApprovalGate:
    def __init__(self) -> None:
        self._future: asyncio.Future | None = None
        self.current_plan: str = ""

    @property
    def is_waiting(self) -> bool:
        return self._future is not None and not self._future.done()

    async def wait(self, plan: str = "") -> dict:
        self.current_plan = plan
        loop = asyncio.get_event_loop()
        self._future = loop.create_future()
        return await self._future

    def resolve(self, approved: bool, feedback: str = "", comments: list[dict] | None = None) -> None:
        if self._future and not self._future.done():
            self._future.set_result({"approved": approved, "feedback": feedback, "comments": comments or []})
