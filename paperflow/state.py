from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class StageRunResult:
    name: str
    command: List[str]
    stdout: str
    stderr: str
    artifacts: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineState:
    watch: Optional[StageRunResult] = None
    dedupe: Optional[StageRunResult] = None
    summary: Optional[StageRunResult] = None
    abstract: Optional[StageRunResult] = None
    notion: Optional[StageRunResult] = None

    def as_dict(self) -> Dict[str, Any]:
        def dump(stage: Optional[StageRunResult]) -> Optional[Dict[str, Any]]:
            if not stage:
                return None
            return {
                "name": stage.name,
                "command": stage.command,
                "artifacts": {
                    key: str(value) if isinstance(value, Path) else value for key, value in stage.artifacts.items()
                },
            }

        return {
            "watch": dump(self.watch),
            "dedupe": dump(self.dedupe),
            "summary": dump(self.summary),
            "abstract": dump(self.abstract),
            "notion": dump(self.notion),
        }
