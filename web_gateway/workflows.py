from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .storage import JobPaths


SUBTITLE_STAGE = "subtitles"
RECAP_PLANNING_STAGE = "recap_planning"
RECAP_RENDER_STAGE = "recap_render"
DEDUP_STAGE = "dedup"
PUBLISHING_PLANNING_STAGE = "publishing_planning"
PUBLISHING_RENDER_STAGE = "publishing_render"


@dataclass(frozen=True)
class WorkflowPlan:
    """A stable, serializable description of the selected processing modules."""

    stages: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "WorkflowPlan":
        pipeline = settings["pipeline"]
        stages: list[str] = []
        if pipeline.get("enable_subtitles"):
            stages.append(SUBTITLE_STAGE)
        if pipeline.get("enable_publishing"):
            stages.append(PUBLISHING_PLANNING_STAGE)
        if pipeline.get("enable_recap"):
            stages.extend((RECAP_PLANNING_STAGE, RECAP_RENDER_STAGE))
        if pipeline.get("enable_dedup"):
            stages.append(DEDUP_STAGE)
        if pipeline.get("enable_publishing"):
            stages.append(PUBLISHING_RENDER_STAGE)
        return cls(tuple(stages))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, "stages": list(self.stages)}


class WorkflowCheckpointStore:
    """Durable stage checkpoints used to resume an interrupted job safely.

    A checkpoint is written only after the stage has produced and validated its
    durable outputs. In-progress work is never treated as complete.
    """

    def __init__(self, paths: JobPaths):
        self.root = paths.root / "checkpoints"
        self.path = self.root / "workflow.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError):
            payload = {}
        stages = payload.get("stages")
        if not isinstance(stages, dict):
            stages = {}
        return {
            "schema_version": 1,
            "updated_at": payload.get("updated_at"),
            "plan": payload.get("plan") if isinstance(payload.get("plan"), dict) else {},
            "stages": stages,
        }

    def initialize(self, plan: WorkflowPlan) -> dict[str, Any]:
        payload = self.read()
        payload["plan"] = plan.to_dict()
        self._write(payload)
        return payload

    def completed(self, stage: str) -> bool:
        record = self.read()["stages"].get(stage)
        return isinstance(record, dict) and record.get("status") == "completed"

    def complete(self, stage: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self.read()
        payload["stages"][stage] = {
            "status": "completed",
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "metadata": dict(metadata or {}),
        }
        self._write(payload)
        return payload

    def invalidate(self, stages: Iterable[str], reason: str) -> dict[str, Any]:
        payload = self.read()
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        for stage in stages:
            if stage in payload["stages"]:
                payload["stages"][stage] = {
                    **payload["stages"][stage],
                    "status": "invalidated",
                    "invalidated_at": timestamp,
                    "reason": reason,
                }
        self._write(payload)
        return payload

    def first_incomplete(self, plan: WorkflowPlan) -> str | None:
        return next((stage for stage in plan.stages if not self.completed(stage)), None)

    def summary(self, plan: WorkflowPlan | None = None) -> dict[str, Any]:
        payload = self.read()
        ordered = list(plan.stages) if plan else list(payload.get("plan", {}).get("stages") or [])
        return {
            **payload,
            "completed_stages": [stage for stage in ordered if self.completed(stage)],
            "next_stage": next((stage for stage in ordered if not self.completed(stage)), None),
        }

    def _write(self, payload: dict[str, Any]) -> None:
        payload["schema_version"] = 1
        payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
