from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import RecapProject, RecapSegment, now_iso


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_project(path: Path) -> RecapProject:
    path = Path(path).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid project JSON: {path}: {exc}") from exc
    project = RecapProject.from_dict(payload)
    if project.schema_version != 1:
        raise ValueError(f"unsupported recap schema_version={project.schema_version}")
    return project


def version_root(project_path: Path, project: RecapProject) -> Path:
    return Path(project_path).resolve().parent / ".recap_versions" / project.project_id


def version_path(project_path: Path, project: RecapProject, version: int) -> Path:
    return version_root(project_path, project) / f"v{version:04d}.json"


def save_project(path: Path, project: RecapProject, *, snapshot: bool = True) -> Path:
    path = Path(path).resolve()
    project.updated_at = now_iso()
    payload = project.to_dict()
    atomic_write_json(path, payload)
    if snapshot:
        atomic_write_json(version_path(path, project, project.current_version), payload)
    return path


def create_project(path: Path, payload: dict[str, Any]) -> RecapProject:
    payload = deepcopy(payload)
    payload.setdefault("schema_version", 1)
    payload.setdefault("current_version", 1)
    payload.setdefault("created_at", now_iso())
    payload.setdefault("updated_at", payload["created_at"])
    project = RecapProject.from_dict(payload)
    save_project(path, project, snapshot=True)
    return project


def save_new_version(path: Path, project: RecapProject) -> RecapProject:
    project.current_version += 1
    save_project(path, project, snapshot=True)
    return project


def update_segment(path: Path, segment_id: str, changes: dict[str, Any]) -> tuple[RecapProject, list[str]]:
    project = load_project(path)
    segment = next((item for item in project.segments if item.segment_id == segment_id), None)
    if segment is None:
        raise KeyError(f"unknown segment_id: {segment_id}")
    allowed = {"episode", "source_start", "source_end", "mode", "narration_text", "purpose", "rendering"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unsupported segment fields: {sorted(unknown)}")
    for key, value in changes.items():
        if key == "episode": value = int(value)
        elif key in {"source_start", "source_end"}: value = float(value)
        elif key == "rendering": value = dict(value or {})
        elif key == "mode": value = str(value).casefold()
        else: value = str(value)
        setattr(segment, key, value)
    segment.revision += 1
    segment.cache_key = ""
    save_new_version(path, project)
    return project, [segment_id]


def delete_segment(path: Path, segment_id: str) -> tuple[RecapProject, list[str]]:
    project = load_project(path)
    before = len(project.segments)
    project.segments = [item for item in project.segments if item.segment_id != segment_id]
    if len(project.segments) == before:
        raise KeyError(f"unknown segment_id: {segment_id}")
    save_new_version(path, project)
    return project, [segment_id]


def load_version(path: Path, version: int) -> RecapProject:
    current = load_project(path)
    candidate = version_path(path, current, version)
    if not candidate.is_file():
        raise FileNotFoundError(f"project version does not exist: {candidate}")
    return RecapProject.from_dict(json.loads(candidate.read_text(encoding="utf-8-sig")))


def diff_versions(path: Path, first: int, second: int) -> dict[str, Any]:
    left = load_version(path, first)
    right = load_version(path, second)
    left_map = {item.segment_id: item.to_dict() for item in left.segments}
    right_map = {item.segment_id: item.to_dict() for item in right.segments}
    return {
        "from_version": first,
        "to_version": second,
        "added": sorted(set(right_map) - set(left_map)),
        "removed": sorted(set(left_map) - set(right_map)),
        "changed": sorted(key for key in set(left_map) & set(right_map) if left_map[key] != right_map[key]),
        "project_fields_changed": sorted(
            key for key in left.to_dict() if key not in {"segments", "current_version", "updated_at"}
            and left.to_dict().get(key) != right.to_dict().get(key)
        ),
    }


def rollback_project(path: Path, version: int) -> RecapProject:
    current = load_project(path)
    restored = load_version(path, version)
    restored.current_version = current.current_version + 1
    restored.created_at = current.created_at
    save_project(path, restored, snapshot=True)
    return restored
