from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .models import RecapProject, RecapSegment


Probe = Callable[[Path], dict[str, Any]]


def validate_project_structure(project: RecapProject) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if not project.project_id:
        errors.append({"code": "missing_project_id", "message": "project_id is required"})
    if not project.project_name:
        errors.append({"code": "missing_project_name", "message": "project_name is required"})
    if not project.segments:
        errors.append({"code": "empty_timeline", "message": "segments cannot be empty"})
    seen: set[str] = set()
    for segment in project.segments:
        if not segment.segment_id:
            errors.append({"code": "missing_segment_id", "message": "segment_id is required"})
        elif segment.segment_id in seen:
            errors.append({"code": "duplicate_segment_id", "segment_id": segment.segment_id, "message": "segment_id must be unique"})
        seen.add(segment.segment_id)
        if segment.mode not in {"narration", "original"}:
            errors.append({"code": "invalid_mode", "segment_id": segment.segment_id, "message": "mode must be narration or original"})
        if segment.episode < 1:
            errors.append({"code": "invalid_episode", "segment_id": segment.segment_id, "message": "episode must be >= 1"})
        if segment.source_start < 0 or segment.source_end <= segment.source_start:
            errors.append({"code": "invalid_interval", "segment_id": segment.segment_id, "interval": [segment.source_start, segment.source_end], "message": "source interval is invalid"})
        if segment.mode == "narration" and not segment.narration_text:
            errors.append({"code": "missing_narration", "segment_id": segment.segment_id, "message": "narration_text is required for narration mode"})
    return errors


def validate_source_intervals(project: RecapProject, probe: Probe) -> list[dict[str, Any]]:
    errors = validate_project_structure(project)
    by_episode: dict[int, list[RecapSegment]] = defaultdict(list)
    episode_info: dict[int, tuple[Path, dict[str, Any]]] = {}
    for segment in project.segments:
        if segment.episode < 1:
            continue
        try:
            source = project.episode_path(segment.episode)
        except (OSError, ValueError) as exc:
            errors.append({"code": "missing_source", "episode": segment.episode, "segment_id": segment.segment_id, "message": str(exc)})
            continue
        if not source.is_file():
            errors.append({"code": "missing_source", "episode": segment.episode, "segment_id": segment.segment_id, "path": str(source), "message": "source video does not exist"})
            continue
        if segment.episode not in episode_info:
            try:
                episode_info[segment.episode] = (source, probe(source))
            except Exception as exc:
                errors.append({"code": "probe_failed", "episode": segment.episode, "path": str(source), "message": str(exc)})
                continue
        duration = float(episode_info[segment.episode][1].get("duration", 0.0))
        if segment.mode == "original" and not bool(episode_info[segment.episode][1].get("has_audio")):
            errors.append({
                "code": "missing_audio_stream", "episode": segment.episode,
                "segment_id": segment.segment_id, "path": str(source),
                "message": "original mode requires a source audio stream",
            })
        if segment.source_end > duration + 0.05:
            errors.append({
                "code": "source_end_out_of_range", "episode": segment.episode,
                "segment_id": segment.segment_id, "source_end": segment.source_end,
                "source_duration": duration,
                "message": f"segment ends at {segment.source_end:.3f}s beyond source duration {duration:.3f}s",
            })
        by_episode[segment.episode].append(segment)

    for episode, items in by_episode.items():
        ordered = sorted(items, key=lambda item: (item.source_start, item.source_end, item.segment_id))
        for index, first in enumerate(ordered):
            for second in ordered[index + 1:]:
                if second.source_start >= first.source_end:
                    break
                overlap = min(first.source_end, second.source_end) - max(first.source_start, second.source_start)
                if overlap > 0:
                    errors.append({
                        "code": "source_interval_overlap", "episode": episode,
                        "first_segment_id": first.segment_id,
                        "first_interval": [first.source_start, first.source_end],
                        "second_segment_id": second.segment_id,
                        "second_interval": [second.source_start, second.source_end],
                        "overlap_seconds": round(overlap, 3),
                        "message": f"episode {episode} source intervals overlap by {overlap:.3f}s",
                    })
    return errors


def output_offsets(manifest: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    running = 0.0
    for item in manifest:
        result[str(item["segment_id"])] = running
        running += float(item["video_seconds"])
    return result


def affected_segments(old: RecapProject, new: RecapProject) -> list[str]:
    old_map = {item.segment_id: item.semantic_payload() for item in old.segments}
    new_map = {item.segment_id: item.semantic_payload() for item in new.segments}
    return sorted(key for key in set(old_map) | set(new_map) if old_map.get(key) != new_map.get(key))
