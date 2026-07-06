from pydantic import BaseModel, Field
from typing import Any, Dict, List, Literal, Optional

class EventInput(BaseModel):
    child_id: str
    ts: int
    kind: str
    url: Optional[str] = None
    title: Optional[str] = None
    tab_id: Optional[str] = None
    referrer: Optional[str] = None
    data_json: Optional[str] = None


class ClientLogEntry(BaseModel):
    ts: str
    level: Literal["debug", "info", "warn", "error"]
    message: str
    context: Optional[Dict[str, Any]] = None


class ClientLogBatch(BaseModel):
    entries: List[ClientLogEntry] = Field(default_factory=list, max_length=500)
