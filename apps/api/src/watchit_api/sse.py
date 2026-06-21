from __future__ import annotations
from typing import AsyncGenerator, Dict, Any
import orjson

def sse_pack(event: Dict[str, Any]) -> bytes:
    payload = orjson.dumps(event)
    return b"data: " + payload + b"\n\n"

async def sse_generator(queue) -> AsyncGenerator[bytes, None]:
    try:
        while True:
            item = await queue.get()
            yield sse_pack(item)
            queue.task_done()
    except Exception:
        return
