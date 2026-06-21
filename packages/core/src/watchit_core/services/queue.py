from __future__ import annotations
import asyncio
from typing import Any, Dict, Callable, Awaitable, Optional

class InprocQueue:
    """Simple in-process async queue for events."""
    def __init__(self):
        self.q: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

    async def put(self, item: Dict[str, Any]):
        await self.q.put(item)

    async def get(self) -> Dict[str, Any]:
        return await self.q.get()

    def task_done(self):
        self.q.task_done()
