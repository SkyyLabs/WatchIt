from __future__ import annotations
from typing import AsyncGenerator, Dict, Any
import orjson

def sse_pack(event: Dict[str, Any]) -> bytes:
    payload = orjson.dumps(event)
    return b"data: " + payload + b"\n\n"

async def sse_generator(
    queue, household_id: str | None = None, device_id: str | None = None
) -> AsyncGenerator[bytes, None]:
    # household_id scopes guardian streams; device_id additionally narrows device
    # streams so a paired browser only ever sees its own decisions (never a
    # sibling's browsing inside the same household).
    try:
        while True:
            item = await queue.get()
            household_ok = household_id is None or item.get("household_id") == household_id
            device_ok = device_id is None or item.get("device_id") == device_id
            if household_ok and device_ok:
                yield sse_pack(item)
            queue.task_done()
    except Exception:
        return
