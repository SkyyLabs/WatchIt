from pydantic import BaseModel
from typing import Optional

class EventInput(BaseModel):
    child_id: str
    ts: int
    kind: str
    url: Optional[str] = None
    title: Optional[str] = None
    tab_id: Optional[str] = None
    referrer: Optional[str] = None
    data_json: Optional[str] = None
