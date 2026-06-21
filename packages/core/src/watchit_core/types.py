from typing import Dict, List, Optional, TypedDict

class AnalysisResult(TypedDict, total=False):
    scores: Dict[str, float]
    label: str
    judge_json: Dict[str, object]  # structured LLM judge output (optional)
