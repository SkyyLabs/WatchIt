"""Device SSE stream must only deliver that device's decisions (gap A4)."""
import asyncio
import json

from watchit_api.sse import sse_generator


def _collect_first(household_id, device_id, items):
    async def run():
        queue = asyncio.Queue()
        for item in items:
            queue.put_nowait(item)
        gen = sse_generator(queue, household_id=household_id, device_id=device_id)
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=2)
        await gen.aclose()
        return json.loads(chunk.decode().removeprefix("data: ").strip())
    return asyncio.run(run())


def test_device_stream_skips_sibling_devices():
    items = [
        {"household_id": "hh_1", "device_id": "dev_sibling", "action": "block"},
        {"household_id": "hh_2", "device_id": "dev_mine", "action": "block"},
        {"household_id": "hh_1", "device_id": "dev_mine", "action": "allow"},
    ]
    first = _collect_first("hh_1", "dev_mine", items)
    assert first["device_id"] == "dev_mine"
    assert first["household_id"] == "hh_1"
    assert first["action"] == "allow"


def test_guardian_stream_still_household_wide():
    items = [
        {"household_id": "hh_2", "device_id": "dev_x", "action": "block"},
        {"household_id": "hh_1", "device_id": "dev_a", "action": "warn"},
    ]
    first = _collect_first("hh_1", None, items)
    assert first["device_id"] == "dev_a"
