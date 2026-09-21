"""Small result dataclasses shared by agent/orchestrator.py and its callers."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class OrchestratorResult:
    mode: Literal["de", "da"]
    answer: str
    tool_calls: list[str] = field(default_factory=list)
    remediated: bool = False
