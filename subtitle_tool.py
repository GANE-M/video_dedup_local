#!/usr/bin/env python3
"""Local subtitle extraction, translation, replacement, and burn-in helpers."""

from __future__ import annotations

import argparse
import ctypes
import difflib
import http.client
import importlib.util
import io
import json
import os
import re
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

import video_dedup
from global_slots import global_llm_slot


def write_translation_record(path: Path | None, record: dict) -> None:
    """Atomically persist translation diagnostics without credentials."""
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_glossary_file(path: Path) -> dict:
    path = Path(path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"术语表根节点必须是 JSON 对象: {path}")
    if int(payload.get("schema_version", 0) or 0) != 1:
        raise ValueError(f"不支持的术语表版本: {path}")
    if not str(payload.get("id", "")).strip() or not str(payload.get("name", "")).strip():
        raise ValueError(f"术语表缺少 id 或 name: {path}")
    terms = payload.get("terms")
    if not isinstance(terms, list) or not terms:
        raise ValueError(f"术语表 terms 必须是非空数组: {path}")
    for index, term in enumerate(terms, 1):
        if not isinstance(term, dict) or any(not str(term.get(language, "")).strip() for language in ("zh", "en", "ar")):
            raise ValueError(f"术语表第 {index} 项必须包含非空 zh/en/ar: {path}")
    result = dict(payload)
    result["_source_path"] = str(path)
    return result


def build_glossary_prompt(glossary: dict | None) -> str:
    if not glossary:
        return ""
    compact_terms = []
    for term in glossary.get("terms", []):
        compact_terms.append(
            {
                "zh": str(term.get("zh", "")).strip(),
                "en": str(term.get("en", "")).strip(),
                "ar": str(term.get("ar", "")).strip(),
                "aliases": term.get("aliases", {}),
                "note": str(term.get("note", "")).strip(),
            }
        )
    glossary_payload = {
        "id": glossary.get("id"),
        "name": glossary.get("name"),
        "genre": glossary.get("genre", ""),
        "description": glossary.get("description", ""),
        "instructions": glossary.get("instructions", []),
        "terms": compact_terms,
    }
    return (
        " A manually selected terminology glossary follows. Apply it only when a source term or alias appears with the meaning described by its note. "
        "For Chinese, English, or Arabic output, use the corresponding zh, en, or ar field exactly unless grammar requires a minimal inflection. "
        "For another target language, use the three fields as semantic references. The selected glossary has priority for matching terms, names, and titles, "
        "but it must not override contradictory context or create terms that are absent. Keep matching terms consistent in every subtitle. "
        "Do not mention or reproduce the glossary outside the translated subtitle. GLOSSARY_JSON="
        + json.dumps(glossary_payload, ensure_ascii=False, separators=(",", ":"))
        + " "
    )


@dataclass
class SubtitleItem:
    index: int
    start: str
    end: str
    text: str


@dataclass
class AlignedSubtitlePair:
    index: int
    start: str
    end: str
    visual_text: str
    audio_text: str
    audio_confidence: float = 0.0
    temporal_confidence: float = 0.0
    confidence_score: float = 0.0
    confidence_reason: str = "unscored"


@dataclass
class AsrWord:
    text: str
    start: float
    end: float
    probability: float = 0.0
    segment: int = 0


LINE_BREAK_MARKER = "⟦BR⟧"
LOG_PREFIX = ""
_NVIDIA_DLL_HANDLES: list[object] = []
_NVIDIA_DLL_LIBRARIES: list[object] = []
TERM_CONSISTENCY_GUIDANCE = (
    "Infer the drama's genre, time period, social setting, and each recurring title from the supplied source and context. "
    "Translate recurring character names, forms of address, organizations, ranks, and culture-specific titles consistently within this video. "
    "Choose a natural target-language equivalent for the actual contextual role; do not force a modern occupation onto a historical title, "
    "and do not force a historical interpretation onto a modern story. Do not invent a glossary that the evidence does not support."
)


def configure_nvidia_dll_directories() -> list[Path]:
    """Expose pip-installed NVIDIA runtime DLLs to Paddle and CTranslate2 on Windows."""
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return []
    root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    directories = sorted(path for path in root.glob("*/bin") if path.is_dir())
    if _NVIDIA_DLL_HANDLES:
        return directories
    for directory in directories:
        try:
            _NVIDIA_DLL_HANDLES.append(os.add_dll_directory(str(directory)))
        except OSError:
            continue
    return directories


def preload_nvidia_runtime_dlls() -> list[Path]:
    directories = configure_nvidia_dll_directories()
    if os.name != "nt" or _NVIDIA_DLL_LIBRARIES:
        return directories
    preferred = (
        "nvjitlink",
        "cuda_runtime",
        "cublas",
        "cufft",
        "curand",
        "cusparse",
        "cusolver",
        "cudnn",
    )
    by_name = {directory.parent.name: directory for directory in directories}
    for package in preferred:
        directory = by_name.get(package)
        if not directory:
            continue
        dlls = sorted(directory.glob("*.dll"), key=lambda path: (path.name != "cudnn64_9.dll", path.name))
        for dll in dlls:
            try:
                _NVIDIA_DLL_LIBRARIES.append(ctypes.WinDLL(str(dll)))
            except OSError:
                continue
    return directories


def isolate_paddlex_from_torch_cuda() -> None:
    """Avoid loading ModelScope/PyTorch CUDA inside a Paddle CUDA OCR process."""
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    if "modelscope" not in sys.modules:
        stub = types.ModuleType("modelscope")
        stub.__path__ = []  # type: ignore[attr-defined]
        sys.modules["modelscope"] = stub


def stage_log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}" if LOG_PREFIX else message)


def run(command: list[str], dry_run: bool = False) -> None:
    print(subprocess.list2cmdline(command))
    if not dry_run:
        completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, **video_dedup.hidden_subprocess_kwargs())
        if completed.stdout:
            print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
        if completed.stderr:
            print(completed.stderr, end="" if completed.stderr.endswith("\n") else "\n", file=sys.stderr)
        if completed.returncode != 0:
            raise subprocess.CalledProcessError(completed.returncode, command, completed.stdout, completed.stderr)


def subtitle_streams(video: Path, ffprobe: str) -> list[dict]:
    command = [ffprobe, "-v", "error", "-select_streams", "s", "-show_streams", "-of", "json", str(video)]
    result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", **video_dedup.hidden_subprocess_kwargs())
    return json.loads(result.stdout).get("streams", [])


def extract_subtitle(video: Path, output: Path, stream: int, ffmpeg: str, dry_run: bool) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-hide_banner", "-y", "-i", str(video), "-map", f"0:s:{stream}", str(output)]
    run(command, dry_run)


def srt_blocks(text: str) -> list[str]:
    return re.split(r"\r?\n\s*\r?\n", text.strip(), flags=re.MULTILINE) if text.strip() else []


def parse_srt(path: Path) -> list[SubtitleItem]:
    raw = path.read_text(encoding="utf-8-sig")
    items: list[SubtitleItem] = []
    for block in srt_blocks(raw):
        lines = [line.strip("\ufeff") for line in block.splitlines()]
        if len(lines) < 2:
            continue
        try:
            index = int(lines[0].strip())
            time_line = lines[1]
            body = "\n".join(lines[2:]).strip()
        except ValueError:
            index = len(items) + 1
            time_line = lines[0]
            body = "\n".join(lines[1:]).strip()
        if "-->" not in time_line:
            continue
        start, end = [part.strip() for part in time_line.split("-->", 1)]
        items.append(SubtitleItem(index=index, start=start, end=end, text=body))
    return items


def write_srt(items: list[SubtitleItem], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for idx, item in enumerate(items, 1):
        blocks.append(f"{idx}\n{item.start} --> {item.end}\n{item.text.strip()}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def clean_and_merge_output_subtitles(
    items: list[SubtitleItem], max_gap_seconds: float = 0.12
) -> tuple[list[SubtitleItem], dict[str, int]]:
    """Drop empty output events and merge only exact adjacent translations."""
    merged: list[SubtitleItem] = []
    removed_empty = 0
    merged_duplicates = 0
    for item in items:
        text = item.text.strip()
        normalized = re.sub(r"\s+", " ", text).casefold()
        if not normalized:
            removed_empty += 1
            continue
        if merged:
            previous = merged[-1]
            previous_normalized = re.sub(r"\s+", " ", previous.text.strip()).casefold()
            gap = srt_time_to_seconds(item.start) - srt_time_to_seconds(previous.end)
            if normalized == previous_normalized and gap <= max_gap_seconds:
                if srt_time_to_seconds(item.end) > srt_time_to_seconds(previous.end):
                    previous.end = item.end
                merged_duplicates += 1
                continue
        merged.append(SubtitleItem(len(merged) + 1, item.start, item.end, text))
    return merged, {
        "input_items": len(items),
        "output_items": len(merged),
        "removed_empty": removed_empty,
        "merged_adjacent_duplicates": merged_duplicates,
    }


def build_series_evidence_catalog(translation_record_paths: list[Path]) -> dict[str, dict]:
    """Load stable episode/index evidence from local translation records."""
    catalog: dict[str, dict] = {}
    for ordinal, record_path in enumerate(translation_record_paths, 1):
        if not record_path.is_file():
            continue
        record = json.loads(record_path.read_text(encoding="utf-8-sig"))
        episode = record.get("video", {}).get("index") or ordinal
        episode_key = str(episode)
        rows: dict[int, dict] = {}
        for position, item in enumerate(record.get("items", []), 1):
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index", position))
            except (TypeError, ValueError):
                index = position
            rows[index] = {
                "source": [
                    str(item.get(key, "")).strip()
                    for key in ("source_clean", "visual_source_clean", "audio_asr_clean")
                    if str(item.get(key, "")).strip()
                ],
                "target": [
                    str(item.get(key, "")).strip()
                    for key in ("initial_translation", "reviewed_translation", "final_translation")
                    if str(item.get(key, "")).strip()
                ],
            }
        reports: list[dict] = []
        for review in record.get("reviews", []):
            report = review.get("report") if isinstance(review, dict) else None
            if isinstance(report, dict) and report.get("entities"):
                reports.append(report)
        for batch in record.get("batches", []):
            report = batch.get("review_report") if isinstance(batch, dict) else None
            if isinstance(report, dict) and report.get("entities"):
                reports.append(report)
        catalog[episode_key] = {
            "episode": episode,
            "record_path": record_path,
            "record": record,
            "rows": rows,
            "reports": reports,
        }
    return catalog


def _entity_pattern(value: str) -> str:
    return rf"(?<!\w){re.escape(value)}(?!\w)"


def _contains_entity(text: str, value: str) -> bool:
    return bool(re.search(_entity_pattern(value), text, flags=re.IGNORECASE | re.UNICODE))


def validate_series_consistency_replacements(
    subtitle_paths: list[Path],
    replacements: list[dict],
    evidence_catalog: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """Verify model claims against real episode/index rows and reject conflicts."""
    allowed_kinds = {"person", "family", "place", "organization", "rank", "title"}
    subtitle_text = "\n".join(item.text for path in subtitle_paths for item in parse_srt(path))
    candidates: list[dict] = []
    rejected: list[dict] = []

    def reject(entry: object, reason: str) -> None:
        rejected.append({"replacement": entry, "reason": reason})

    for entry in replacements:
        if not isinstance(entry, dict):
            reject(entry, "replacement 不是对象")
            continue
        kind = str(entry.get("kind", "")).strip().casefold()
        old = clean_translated_text(str(entry.get("from", "")))
        new = clean_translated_text(str(entry.get("to", "")))
        try:
            confidence = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if kind not in allowed_kinds or not old or not new or old.casefold() == new.casefold():
            reject(entry, "类型或 from/to 无效")
            continue
        if confidence < 0.85 or len(old) > 120 or len(new) > 120:
            reject(entry, "置信度不足或名称过长")
            continue
        evidence = entry.get("evidence")
        if not isinstance(evidence, list):
            reject(entry, "缺少可核验的 episode/index 证据")
            continue
        verified_episodes: set[str] = set()
        evidence_valid = True
        for reference in evidence:
            if not isinstance(reference, dict):
                evidence_valid = False
                break
            episode_key = str(reference.get("episode", ""))
            indexes = reference.get("indexes")
            episode = evidence_catalog.get(episode_key)
            if episode is None or not isinstance(indexes, list) or not indexes:
                evidence_valid = False
                break
            for raw_index in indexes:
                try:
                    evidence_index = int(raw_index)
                    row = episode["rows"][evidence_index]
                except (TypeError, ValueError, KeyError):
                    evidence_valid = False
                    break
                target_values = row.get("target", [])
                if not any(_contains_entity(value, old) or _contains_entity(value, new) for value in target_values):
                    evidence_valid = False
                    break
                entity_support = False
                for report in episode.get("reports", []):
                    for entity in report.get("entities", []):
                        if not isinstance(entity, dict) or str(entity.get("kind", "")).strip().casefold() != kind:
                            continue
                        try:
                            entity_indexes = {int(value) for value in entity.get("evidence_indexes", [])}
                        except (TypeError, ValueError):
                            continue
                        variants = [
                            clean_translated_text(str(value))
                            for value in entity.get("target_variants", [])
                        ]
                        preferred = clean_translated_text(str(entity.get("preferred_target", "")))
                        if preferred:
                            variants.append(preferred)
                        if evidence_index in entity_indexes and any(
                            value.casefold() in {old.casefold(), new.casefold()} for value in variants if value
                        ):
                            entity_support = True
                            break
                    if entity_support:
                        break
                if not entity_support:
                    evidence_valid = False
                    break
            if not evidence_valid:
                break
            verified_episodes.add(episode_key)
        if not evidence_valid or len(verified_episodes) < 2:
            reject(entry, "证据索引不存在、对应字幕不含名称，或不足两集")
            continue
        if not _contains_entity(subtitle_text, old):
            reject(entry, "待替换名称未出现在终稿字幕")
            continue
        candidates.append(
            {
                "kind": kind,
                "from": old,
                "to": new,
                "confidence": confidence,
                "verified_episode_count": len(verified_episodes),
                "evidence": evidence,
            }
        )

    from_targets: dict[str, set[str]] = {}
    for entry in candidates:
        from_targets.setdefault(entry["from"].casefold(), set()).add(entry["to"].casefold())
    source_names = set(from_targets)
    target_names = {entry["to"].casefold() for entry in candidates}
    validated: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for entry in candidates:
        old_key = entry["from"].casefold()
        new_key = entry["to"].casefold()
        if len(from_targets[old_key]) != 1:
            reject(entry, "同一 from 指向多个规范名")
            continue
        if new_key in source_names or old_key in target_names:
            reject(entry, "检测到反向、循环或链式替换")
            continue
        pair = (old_key, new_key)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        validated.append(entry)
    return validated, rejected


def _apply_simultaneous_entity_replacements(text: str, replacements: list[dict]) -> tuple[str, int]:
    if not replacements or not text:
        return text, 0
    mapping = {entry["from"].casefold(): entry["to"] for entry in replacements}
    alternatives = "|".join(re.escape(entry["from"]) for entry in sorted(replacements, key=lambda row: len(row["from"]), reverse=True))
    pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", flags=re.IGNORECASE | re.UNICODE)
    return pattern.subn(lambda match: mapping[match.group(0).casefold()], text)


def apply_series_consistency_replacements(
    subtitle_paths: list[Path],
    replacements: list[dict],
    evidence_catalog: dict[str, dict] | None = None,
) -> dict:
    """Apply only locally verified replacements in one simultaneous pass."""
    validated, rejected = validate_series_consistency_replacements(
        subtitle_paths, replacements, evidence_catalog or {}
    )
    changed_files = 0
    changed_occurrences = 0
    for path in subtitle_paths:
        items = parse_srt(path)
        file_changes = 0
        for item in items:
            item.text, count = _apply_simultaneous_entity_replacements(item.text, validated)
            file_changes += count
        if file_changes:
            write_srt(items, path)
            changed_files += 1
            changed_occurrences += file_changes
    return {
        "requested_replacements": len(replacements),
        "validated_replacements": len(validated),
        "validated": validated,
        "rejected_replacements": len(rejected),
        "rejected": rejected,
        "changed_files": changed_files,
        "changed_occurrences": changed_occurrences,
    }


def sync_series_replacements_to_records(
    evidence_catalog: dict[str, dict], validated: list[dict]
) -> dict[str, int]:
    """Keep per-episode JSON final_translation fields aligned with final SRTs."""
    changed_records = 0
    changed_occurrences = 0
    for episode in evidence_catalog.values():
        record = episode["record"]
        record_changes = 0
        for item in record.get("items", []):
            if not isinstance(item, dict):
                continue
            value = item.get("final_translation")
            if isinstance(value, str):
                item["final_translation"], count = _apply_simultaneous_entity_replacements(value, validated)
                record_changes += count
        for batch in record.get("batches", []):
            values = batch.get("final_translation") if isinstance(batch, dict) else None
            if isinstance(values, list):
                batch["final_translation"] = [
                    _apply_simultaneous_entity_replacements(value, validated)[0] if isinstance(value, str) else value
                    for value in values
                ]
        record["series_consistency"] = {
            "status": "applied" if record_changes else "checked",
            "replacements": [{"kind": row["kind"], "from": row["from"], "to": row["to"]} for row in validated],
            "changed_occurrences": record_changes,
        }
        write_translation_record(episode["record_path"], record)
        changed_records += bool(record_changes)
        changed_occurrences += record_changes
    return {"changed_records": changed_records, "changed_occurrences": changed_occurrences}


def apply_episode_review_edits(
    timed_items: list[SubtitleItem | AlignedSubtitlePair],
    initial_translation: list[str],
    edits: list[dict],
    max_merge_seconds: float = 6.0,
) -> tuple[list[str], dict[str, int]]:
    """Validate sparse edits and stop one bad model response damaging an episode."""
    if len(timed_items) != len(initial_translation):
        raise ValueError("审核编辑输入数量不一致。")
    if not timed_items:
        return [], {"replace": 0, "merge": 0, "delete": 0, "invalid": 0, "safety_rejected": 0}
    index_to_position = {item.index: position for position, item in enumerate(timed_items)}
    final = list(initial_translation)
    occupied: set[int] = set()
    applied = {"replace": 0, "merge": 0, "delete": 0, "invalid": 0, "safety_rejected": 0}
    total_items = len(timed_items)
    # Initial translation should already be usable. The reviewer is a repair
    # layer, so a response trying to rewrite most of an episode is unsafe.
    # Allow one short fragment group even in a tiny test/clip, while long
    # episodes still cap the reviewer's total rewrite surface at about 40%.
    # This is deliberately independent from the stricter deletion limits below.
    max_affected_items = max(8, (total_items * 40 + 99) // 100)
    max_deleted_items = max(1, (total_items + 7) // 8)  # at most about 12.5%
    episode_start = min(srt_time_to_seconds(item.start) for item in timed_items)
    episode_end = max(srt_time_to_seconds(item.end) for item in timed_items)
    max_deleted_seconds = max(2.0, min(15.0, (episode_end - episode_start) * 0.15))
    affected_items = 0
    deleted_items = 0
    deleted_seconds = 0.0
    for edit in edits:
        if not isinstance(edit, dict):
            applied["invalid"] += 1
            continue
        action = str(edit.get("action", "")).strip().casefold()
        raw_indexes = edit.get("indexes")
        if not isinstance(raw_indexes, list) or not raw_indexes:
            applied["invalid"] += 1
            continue
        try:
            source_indexes = [int(value) for value in raw_indexes]
            positions = [index_to_position[value] for value in source_indexes]
        except (TypeError, ValueError, KeyError):
            applied["invalid"] += 1
            continue
        if len(set(positions)) != len(positions) or any(position in occupied for position in positions):
            applied["invalid"] += 1
            continue
        positions = sorted(positions)
        if positions != list(range(positions[0], positions[-1] + 1)):
            applied["invalid"] += 1
            continue
        text = clean_translated_text(str(edit.get("text", "")))
        if action == "replace":
            if len(positions) != 1 or not text or len(text) > 300:
                applied["invalid"] += 1
                continue
            if affected_items + 1 > max_affected_items:
                applied["safety_rejected"] += 1
                continue
            final[positions[0]] = text
        elif action == "merge":
            duration = srt_time_to_seconds(timed_items[positions[-1]].end) - srt_time_to_seconds(
                timed_items[positions[0]].start
            )
            if len(positions) < 2 or len(positions) > 8 or not text or len(text) > 300 or duration > max_merge_seconds:
                applied["invalid"] += 1
                continue
            if affected_items + len(positions) > max_affected_items:
                applied["safety_rejected"] += 1
                continue
            for position in positions:
                final[position] = text
        elif action == "delete":
            duration = srt_time_to_seconds(timed_items[positions[-1]].end) - srt_time_to_seconds(
                timed_items[positions[0]].start
            )
            if duration > 4.0:
                applied["safety_rejected"] += 1
                continue
            if (
                affected_items + len(positions) > max_affected_items
                or deleted_items + len(positions) > max_deleted_items
                or deleted_seconds + max(0.0, duration) > max_deleted_seconds
            ):
                applied["safety_rejected"] += 1
                continue
            for position in positions:
                final[position] = ""
            deleted_items += len(positions)
            deleted_seconds += max(0.0, duration)
        else:
            applied["invalid"] += 1
            continue
        occupied.update(positions)
        affected_items += len(positions)
        applied[action] += 1
    return final, applied


def review_series_consistency_openai_compatible(
    translation_record_paths: list[Path],
    subtitle_paths: list[Path],
    target_language: str,
    model: str,
    report_path: Path | None = None,
) -> dict:
    """Reconcile LLM-discovered entities across every episode in one task."""
    evidence_catalog = build_series_evidence_catalog(translation_record_paths)
    episode_evidence: list[dict] = []
    for episode in evidence_catalog.values():
        reports = episode["reports"]
        if reports:
            referenced_indexes: set[int] = set()
            for report in reports:
                for entity in report.get("entities", []):
                    if not isinstance(entity, dict):
                        continue
                    for value in entity.get("evidence_indexes", []):
                        try:
                            referenced_indexes.add(int(value))
                        except (TypeError, ValueError):
                            continue
            episode_evidence.append(
                {
                    "episode": episode["episode"],
                    "video": episode["record"].get("video", {}).get("input"),
                    "visual_language": episode["record"].get("pipeline", {}).get("visual_language")
                    or episode["record"].get("pipeline", {}).get("source_language"),
                    "review_reports": reports,
                    "evidence_rows": [
                        {"index": index, **episode["rows"][index]}
                        for index in sorted(referenced_indexes)
                        if index in episode["rows"]
                    ],
                }
            )

    report: dict = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "skipped",
        "model": model,
        "target_language": target_language,
        "episodes_with_entity_evidence": len(episode_evidence),
        "consistency": {},
        "replacements": [],
        "apply_stats": {},
    }
    if not episode_evidence:
        report["reason"] = "整集审核未返回实体证据"
        if report_path:
            write_translation_record(report_path, report)
        print("全剧一致性审核跳过：整集审核未返回实体证据。")
        return report

    prompt = (
        "You are the series-level consistency reviewer for a folder of short-drama episodes. Episode reviewers have already discovered "
        "possible people, families, places, organizations, ranks, and titles from synchronized OCR/ASR evidence. Treat every episode entity "
        "report as an untrusted hypothesis: episode reviewers may accidentally group different characters. Reconcile only those "
        "semantic entities across the whole series. Do not translate dialogue and do not invent entities. Prefer the localized names visibly "
        "used by a reliable embedded/visual subtitle track when that helps bilingual viewers match both lines; otherwise choose the best-supported "
        "recurring form. Produce exact target-language variant replacements only when two forms clearly denote the same entity. Never replace "
        "ordinary vocabulary, pronouns, particles, or dialogue fragments. Never merge two people merely because they share a surname, role, scene, "
        "or similar spelling. A replacement is eligible only with confidence >= 0.85 and supporting evidence from at least two separate episodes. "
        "Every replacement must cite the exact episode and subtitle indexes where either the old target variant or canonical target variant is visible. "
        "Do not claim an episode or index that is absent from evidence_rows; the local program verifies every citation and rejects unsupported claims. "
        "If this standard is not met, record the uncertainty but emit no replacement. Each replacement must have kind person, family, place, "
        "organization, rank, or title; from and to must both be target-language strings. Return JSON only with an empty subtitles array and this shape: "
        '{"review":{"summary":"..."},"consistency":{"decisions":[{"kind":"person","source_aliases":["..."],'
        '"canonical_target":"...","reason":"..."}],"replacements":[{"kind":"person","from":"...","to":"...",'
        '"confidence":0.9,"evidence":[{"episode":1,"indexes":[12,24]},{"episode":2,"indexes":[7]}],"reason":"..."}]},"subtitles":[]}.'
    )
    result = chat_json_object_openai_compatible(
        prompt=prompt,
        user_payload={
            "task": "reconcile_series_entities",
            "target_language": target_language,
            "episode_count": len(episode_evidence),
            "episodes": episode_evidence,
        },
        model=model,
        expected_count=0,
        log_label="AI 全剧一致性审核",
    )
    consistency = result.get("consistency") if isinstance(result.get("consistency"), dict) else {}
    replacements = consistency.get("replacements") if isinstance(consistency.get("replacements"), list) else []
    apply_stats = apply_series_consistency_replacements(subtitle_paths, replacements, evidence_catalog)
    record_sync_stats = sync_series_replacements_to_records(
        evidence_catalog, apply_stats.get("validated", [])
    )
    report.update(
        {
            "status": "completed",
            "review": result.get("review", {}),
            "consistency": consistency,
            "replacements": replacements,
            "apply_stats": apply_stats,
            "record_sync_stats": record_sync_stats,
        }
    )
    if report_path:
        write_translation_record(report_path, report)
    print(
        "全剧一致性审核完成: "
        f"entities={len(consistency.get('decisions', [])) if isinstance(consistency.get('decisions'), list) else 0}, "
        f"replacements={apply_stats['validated_replacements']}, rejected={apply_stats['rejected_replacements']}, "
        f"changed={apply_stats['changed_occurrences']}, synced_records={record_sync_stats['changed_records']}"
    )
    return report


def align_visual_and_audio_subtitles(
    visual_items: list[SubtitleItem],
    audio_items: list[SubtitleItem],
    max_gap_seconds: float = 0.8,
    audio_words: list[AsrWord] | None = None,
) -> list[AlignedSubtitlePair]:
    """Align OCR/soft subtitles with ASR while preserving the visual timeline."""
    if audio_words:
        return align_visual_with_asr_words(visual_items, audio_words, min(0.4, max_gap_seconds))
    rows: list[dict] = []
    for item in visual_items:
        rows.append(
            {
                "start": srt_time_to_seconds(item.start),
                "end": srt_time_to_seconds(item.end),
                "visual_text": item.text.strip(),
                "audio_texts": [],
                "audio_confidences": [],
            }
        )

    unmatched_audio: list[SubtitleItem] = []
    for audio in audio_items:
        audio_start = srt_time_to_seconds(audio.start)
        audio_end = srt_time_to_seconds(audio.end)
        audio_center = (audio_start + audio_end) / 2
        best_index: int | None = None
        best_score: tuple[int, float] | None = None
        for index, row in enumerate(rows):
            overlap = max(0.0, min(audio_end, row["end"]) - max(audio_start, row["start"]))
            row_center = (row["start"] + row["end"]) / 2
            distance = abs(audio_center - row_center)
            if overlap > 0:
                score = (2, overlap)
            elif distance <= max_gap_seconds:
                score = (1, -distance)
            else:
                continue
            if best_score is None or score > best_score:
                best_index = index
                best_score = score
        if best_index is None:
            unmatched_audio.append(audio)
            continue
        row = rows[best_index]
        text = audio.text.strip()
        if text and text not in row["audio_texts"]:
            row["audio_texts"].append(text)
            row["audio_confidences"].append(0.65)

    for audio in unmatched_audio:
        rows.append(
            {
                "start": srt_time_to_seconds(audio.start),
                "end": srt_time_to_seconds(audio.end),
                "visual_text": "",
                "audio_texts": [audio.text.strip()] if audio.text.strip() else [],
                "audio_confidences": [0.65] if audio.text.strip() else [],
            }
        )

    rows.sort(key=lambda row: (row["start"], row["end"]))
    return [
        AlignedSubtitlePair(
            index=index,
            start=seconds_to_srt_time(row["start"]),
            end=seconds_to_srt_time(row["end"]),
            visual_text=row["visual_text"],
            audio_text=" ".join(row["audio_texts"]).strip(),
            audio_confidence=(statistics.fmean(row["audio_confidences"]) if row["audio_confidences"] else 0.0),
            temporal_confidence=0.55 if row["audio_texts"] else 0.0,
        )
        for index, row in enumerate(rows, 1)
        if row["visual_text"] or row["audio_texts"]
    ]


def load_asr_words(path: Path | None) -> list[AsrWord]:
    if not path or not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("words", []) if isinstance(payload, dict) else payload
    words: list[AsrWord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text", ""))
        try:
            start = float(row.get("start"))
            end = float(row.get("end"))
            probability = float(row.get("probability", 0.0) or 0.0)
            segment = int(row.get("segment", 0) or 0)
        except (TypeError, ValueError):
            continue
        if text.strip() and end >= start:
            words.append(AsrWord(text, start, end, max(0.0, min(1.0, probability)), segment))
    return words


def join_asr_words(words: Sequence[AsrWord]) -> str:
    value = ""
    for word in words:
        token = word.text
        stripped = token.strip()
        if not stripped:
            continue
        if not value:
            value = stripped
        elif token[:1].isspace() or stripped[:1] in ".,!?;:，。！？；：、)]}»”'\"":
            value += token if token[:1].isspace() else stripped
        elif value[-1:].isascii() and stripped[:1].isascii() and value[-1:].isalnum() and stripped[:1].isalnum():
            value += " " + stripped
        else:
            value += stripped
    return re.sub(r"\s+", " ", value).strip()


def align_visual_with_asr_words(
    visual_items: list[SubtitleItem],
    words: list[AsrWord],
    tolerance_seconds: float = 0.35,
) -> list[AlignedSubtitlePair]:
    rows = [
        {
            "start": srt_time_to_seconds(item.start),
            "end": srt_time_to_seconds(item.end),
            "visual_text": item.text.strip(),
            "words": [],
            "temporal_scores": [],
        }
        for item in visual_items
    ]
    unmatched: list[AsrWord] = []
    for word in words:
        center = (word.start + word.end) / 2
        duration = max(0.01, word.end - word.start)
        candidates: list[tuple[tuple[int, float, float], int, float]] = []
        for index, row in enumerate(rows):
            overlap = max(0.0, min(word.end, row["end"]) - max(word.start, row["start"]))
            row_center = (row["start"] + row["end"]) / 2
            distance = abs(center - row_center)
            inside = row["start"] <= center <= row["end"]
            if overlap <= 0 and not (row["start"] - tolerance_seconds <= center <= row["end"] + tolerance_seconds):
                continue
            temporal = 1.0 if inside else max(0.0, 1.0 - min(abs(center - row["start"]), abs(center - row["end"])) / tolerance_seconds)
            candidates.append(((1 if inside else 0, overlap / duration, -distance), index, temporal))
        if not candidates:
            unmatched.append(word)
            continue
        _score, best_index, temporal = max(candidates, key=lambda item: item[0])
        rows[best_index]["words"].append(word)
        rows[best_index]["temporal_scores"].append(temporal)

    pairs: list[AlignedSubtitlePair] = []
    for row in rows:
        assigned: list[AsrWord] = row["words"]
        pairs.append(
            AlignedSubtitlePair(
                0,
                seconds_to_srt_time(row["start"]),
                seconds_to_srt_time(row["end"]),
                row["visual_text"],
                join_asr_words(assigned),
                statistics.fmean(word.probability for word in assigned) if assigned else 0.0,
                statistics.fmean(row["temporal_scores"]) if row["temporal_scores"] else 0.0,
            )
        )

    # Preserve speech that has no visual subtitle. Group adjacent unmatched
    # words from the same Whisper segment into an ASR-only subtitle.
    groups: list[list[AsrWord]] = []
    for word in unmatched:
        if not groups or word.segment != groups[-1][-1].segment or word.start - groups[-1][-1].end > 1.0:
            groups.append([word])
        else:
            groups[-1].append(word)
    for group in groups:
        pairs.append(
            AlignedSubtitlePair(
                0,
                seconds_to_srt_time(group[0].start),
                seconds_to_srt_time(group[-1].end),
                "",
                join_asr_words(group),
                statistics.fmean(word.probability for word in group),
                0.75,
            )
        )
    pairs = [pair for pair in pairs if pair.visual_text or pair.audio_text]
    pairs.sort(key=lambda pair: (srt_time_to_seconds(pair.start), srt_time_to_seconds(pair.end)))
    for index, pair in enumerate(pairs, 1):
        pair.index = index
    return pairs


def language_key(value: str) -> str:
    normalized = (value or "auto").strip().casefold()
    aliases = {
        "en": "english", "英语": "english", "英文": "english",
        "zh": "chinese", "ch": "chinese", "中文": "chinese",
        "ar": "arabic", "arabic": "arabic", "阿拉伯语": "arabic",
    }
    return aliases.get(normalized, normalized)


def source_text_quality(text: str, source_kind: str) -> float:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    cleaned = clean_text_for_translation(raw, source_kind)
    if not cleaned:
        return 0.0
    compact = re.sub(r"\s+", "", cleaned)
    if len(compact) <= 2:
        return 0.15
    useful = sum(char.isalnum() or char.isspace() or char in "'’.,!?،؟" for char in cleaned)
    quality = useful / max(1, len(cleaned))
    if re.search(r"\b(?:[A-Za-z]|\d)\b", cleaned):
        quality -= 0.2
    if source_kind == "soft":
        quality = min(1.0, quality + 0.08)
    return max(0.0, min(1.0, quality))


def allow_review_to_delete_subtitle(pair: AlignedSubtitlePair, visual_kind: str = "ocr") -> bool:
    """Allow an LLM reviewer to blank a subtitle only with strong noise evidence."""
    visual = clean_text_for_translation(pair.visual_text, visual_kind).strip()
    audio = clean_text_for_translation(pair.audio_text, "asr").strip()
    if not visual and not audio:
        return True

    # A non-empty ASR result is speech evidence. Even a short word may be real,
    # so a reviewer may rewrite it but must not silently delete the event.
    if audio:
        return False

    # With no ASR evidence, permit deletion only for unmistakable OCR debris.
    if not visual:
        return True
    compact = re.sub(r"\s+", "", visual)
    if not any(char.isalnum() for char in compact):
        return True
    if re.fullmatch(r"[A-Z0-9_]{1,3}", compact):
        # Preserve short English dialogue that can legitimately appear alone.
        return compact not in {"OK", "NO", "GO", "HI", "YES", "WHY"}
    return False


def score_aligned_pair(
    pair: AlignedSubtitlePair,
    visual_language: str,
    audio_language: str,
    visual_kind: str = "ocr",
) -> AlignedSubtitlePair:
    visual = clean_text_for_translation(pair.visual_text, visual_kind)
    audio = clean_text_for_translation(pair.audio_text, "asr")
    visual_quality = source_text_quality(pair.visual_text, visual_kind)
    asr_quality = max(0.0, min(1.0, pair.audio_confidence))
    temporal = max(0.0, min(1.0, pair.temporal_confidence))
    if visual and audio:
        same_language = language_key(visual_language) == language_key(audio_language) or "auto" in {
            language_key(visual_language), language_key(audio_language)
        }
        if same_language:
            left = normalize_ocr_text(visual).casefold()
            right = normalize_ocr_text(audio).casefold()
            agreement = difflib.SequenceMatcher(None, left, right).ratio() if left and right else 0.0
            score = 0.40 * agreement + 0.20 * visual_quality + 0.25 * asr_quality + 0.15 * temporal
            reason = f"agreement={agreement:.2f},ocr={visual_quality:.2f},asr={asr_quality:.2f},time={temporal:.2f}"
        else:
            # Text similarity is meaningless across languages. Score the
            # independent evidence quality, time alignment, and OCR stability
            # instead of forcing every English+Chinese pair below the review
            # threshold. Very short OCR segments are often transient garble.
            duration = max(0.0, srt_time_to_seconds(pair.end) - srt_time_to_seconds(pair.start))
            stability = 1.0 if visual_kind == "soft" else min(1.0, duration / 1.0)
            score = 0.20 * visual_quality + 0.25 * asr_quality + 0.25 * temporal + 0.30 * stability
            if visual_quality < 0.40:
                score = min(score, 0.79)
            reason = (
                f"cross-language,visual={visual_quality:.2f},"
                f"asr={asr_quality:.2f},time={temporal:.2f},stability={stability:.2f}"
            )
    elif visual:
        multiplier = 0.90 if visual_kind == "soft" else 0.78
        score = multiplier * visual_quality
        reason = f"visual-only,{visual_kind}={visual_quality:.2f}"
    elif audio:
        score = 0.82 * asr_quality + 0.08 * temporal
        reason = f"asr-only,asr={asr_quality:.2f},time={temporal:.2f}"
    else:
        score = 0.0
        reason = "empty sources"
    pair.confidence_score = round(max(0.0, min(1.0, score)), 4)
    pair.confidence_reason = reason
    return pair


def protect_line_breaks_for_llm(text: str) -> str:
    # Only protect the normalized subtitle line-feed inside parsed SRT text.
    # Do not replace "\r"; SRT parsing has already normalized real subtitle
    # line breaks to "\n", and keeping the operation one-way avoids ambiguity.
    return text.replace("\n", LINE_BREAK_MARKER)


def restore_line_breaks_from_llm(text: str) -> str:
    return text.replace(LINE_BREAK_MARKER, "\n")


def clean_text_for_translation(text: str, source_kind: str = "ocr") -> str:
    value = text.replace("\n", " ")
    value = re.sub(r"\s+", " ", value).strip()
    if source_kind == "ocr":
        value = strip_isolated_ocr_digits(value)
        if re.fullmatch(r"[\W_0-9\u0660-\u0669]+", value, flags=re.UNICODE):
            return ""
    return value


def clean_translated_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def has_arabic_text(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06ff]", text))


def isolated_digit_count(text: str) -> int:
    return len(re.findall(r"(?<![\w\u0600-\u06ff])[\d\u0660-\u0669](?![\w\u0600-\u06ff])", text, flags=re.UNICODE))


def strip_isolated_ocr_digits(text: str) -> str:
    value = text
    # Arabic hard-subtitle OCR often hallucinates isolated 5/9/٤/٥ marks from
    # strokes, punctuation, compression blocks, or nearby UI. Remove only
    # standalone one-digit tokens; keep multi-digit numbers such as 10/100/2026.
    if has_arabic_text(value) or isolated_digit_count(value) >= 2:
        value = re.sub(r"(?<![\w\u0600-\u06ff])[\d\u0660-\u0669](?![\w\u0600-\u06ff])", " ", value, flags=re.UNICODE)
        value = re.sub(r"\s+", " ", value).strip()
    return value


def fetch_chat_completion_json(url: str, request_data: bytes, api_key: str, timeout_seconds: int) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) video-dedup-local/1.0",
    }
    request = urllib.request.Request(url, data=request_data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except UnicodeEncodeError as exc:
        try:
            import requests
        except ImportError:
            raise RuntimeError("AI 请求编码失败，且未安装 requests，无法切换到直连请求模式。") from exc
        for name, value in headers.items():
            try:
                value.encode("latin-1")
            except UnicodeEncodeError as header_exc:
                raise RuntimeError(f"AI 请求头 {name} 包含非 ASCII/latin-1 字符；请检查 API Key 或环境变量。") from header_exc
        print("AI 请求遇到 urllib 编码问题，自动改用 requests 直连模式。")
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.post(url, data=request_data, headers=headers, timeout=timeout_seconds)
        except requests.exceptions.Timeout as timeout_exc:
            raise socket.timeout(str(timeout_exc)) from timeout_exc
        except requests.exceptions.RequestException as request_exc:
            raise urllib.error.URLError(str(request_exc)) from request_exc
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                url,
                response.status_code,
                response.text,
                response.headers,
                io.BytesIO(response.content),
            )
        return response.json()


def fetch_chat_completion_json_with_slot(
    url: str,
    request_data: bytes,
    api_key: str,
    timeout_seconds: int,
    label: str,
    slot_limit: int | None = None,
) -> dict:
    """Send one request while respecting the machine-wide LLM concurrency cap."""
    if slot_limit is None:
        try:
            limit = max(1, int(os.environ.get("VIDEO_DEDUP_GLOBAL_LLM_WORKERS", "5")))
        except ValueError:
            limit = 5
    else:
        limit = max(1, int(slot_limit))
    with global_llm_slot(limit, label):
        return fetch_chat_completion_json(url, request_data, api_key, timeout_seconds)


def wrap_subtitle_text(text: str, max_chars: int, max_lines: int = 2) -> str:
    value = clean_translated_text(text)
    if not value or len(value) <= max_chars:
        return value
    words = value.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) >= max_lines - 1:
            break
    remaining_words = words[sum(len(line.split(" ")) for line in lines):]
    if remaining_words:
        current = " ".join(remaining_words)
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return "\n".join(lines)


def subtitle_line_char_limit(video_width: int, cover_width_percent: float, font_size: int) -> int:
    effective_width = max(120.0, video_width * max(10.0, min(100.0, cover_width_percent)) / 100)
    # Latin subtitle text at typical libass fonts averages roughly 0.75-0.85em
    # per character. Use a conservative divisor so lines stay inside the mask.
    return max(18, min(56, int(effective_width / max(12, font_size) / 0.8)))


def prepare_srt_for_render(input_srt: Path, output_srt: Path, video_width: int, cover_width_percent: float, font_size: int) -> Path:
    items = parse_srt(input_srt)
    limit = subtitle_line_char_limit(video_width, cover_width_percent, font_size)
    formatted = [
        SubtitleItem(item.index, item.start, item.end, wrap_subtitle_text(item.text, limit))
        for item in items
    ]
    write_srt(formatted, output_srt)
    print(f"字幕自动排版: 每行约 {limit} 字符，最多 2 行 -> {output_srt}")
    return output_srt


def prepare_items_for_ass_render(input_srt: Path, video_width: int, cover_width_percent: float, font_size: int) -> list[SubtitleItem]:
    items = parse_srt(input_srt)
    limit = subtitle_line_char_limit(video_width, cover_width_percent, font_size)
    formatted = [
        SubtitleItem(item.index, item.start, item.end, wrap_subtitle_text(item.text, limit))
        for item in items
    ]
    print(f"字幕自动排版: 每行约 {limit} 字符，最多 2 行")
    return formatted


def seconds_to_ass_time(value: float) -> str:
    total_centis = int(round(max(0.0, float(value)) * 100))
    hours, remainder = divmod(total_centis, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def srt_time_to_ass_time(value: str) -> str:
    return seconds_to_ass_time(srt_time_to_seconds(value))


def escape_ass_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def ass_position_override(x: float, y: float, alignment: int = 5) -> str:
    return "{\\an" + str(alignment) + r"\pos(" + f"{x:.1f},{y:.1f}" + ")}"


def sanitize_ass_font_name(font_name: str) -> str:
    value = (font_name or "Arial").strip()
    # ASS style lines are comma-separated. Font family names normally do not
    # contain commas; strip them to avoid corrupting the style fields.
    value = value.replace(",", " ")
    return re.sub(r"\s+", " ", value).strip() or "Arial"


def subtitle_ass_style(
    layout: str,
    font_size: int,
    position: str,
    video_width: int,
    cover_x_percent: float,
    cover_width_percent: float,
) -> tuple[int, int, int, int]:
    if position == "auto":
        position = "top" if layout == "bilingual" else "bottom"
    if position == "top":
        alignment, margin_v = 8, 45
    elif position == "above-original":
        alignment, margin_v = 2, 150
    else:
        alignment, margin_v = 2, 40
    margin_l = 20
    margin_r = 20
    # The manually selected rectangle is the subtitle layout area for both
    # replace and bilingual modes. In bilingual mode it constrains the new
    # subtitle without causing the white cover mask to be drawn.
    if video_width > 0:
        safe_x = max(0.0, min(99.0, cover_x_percent))
        safe_w = max(1.0, min(100.0 - safe_x, cover_width_percent))
        margin_l = max(20, int(video_width * safe_x / 100))
        margin_r = max(20, int(video_width * max(0.0, 100.0 - safe_x - safe_w) / 100))
    return alignment, margin_l, margin_r, margin_v


def write_ass_for_render(
    items: Sequence[SubtitleItem],
    output_ass: Path,
    video_width: int,
    video_height: int,
    layout: str,
    font_name: str,
    font_size: int,
    position: str,
    cover_x_percent: float,
    cover_y_percent: float,
    cover_width_percent: float,
    cover_height_percent: float,
) -> Path:
    output_ass.parent.mkdir(parents=True, exist_ok=True)
    alignment, margin_l, margin_r, margin_v = subtitle_ass_style(
        layout,
        font_size,
        position,
        video_width,
        cover_x_percent,
        cover_width_percent,
    )
    font_name = sanitize_ass_font_name(font_name)
    header = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: {max(1, int(video_width))}
PlayResY: {max(1, int(video_height))}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{int(font_size)},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,1,{alignment},{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    event_lines = []
    safe_x = max(0.0, min(99.0, cover_x_percent))
    safe_y = max(0.0, min(99.0, cover_y_percent))
    safe_w = max(1.0, min(100.0 - safe_x, cover_width_percent))
    safe_h = max(1.0, min(100.0 - safe_y, cover_height_percent))
    center_x = video_width * (safe_x + safe_w / 2) / 100
    top_y = video_height * safe_y / 100
    bottom_y = video_height * (safe_y + safe_h) / 100
    if layout == "replace":
        override = ass_position_override(center_x, (top_y + bottom_y) / 2)
    else:
        resolved_position = "top" if position == "auto" else position
        padding = max(6.0, font_size * 0.5)
        if resolved_position == "top":
            override = ass_position_override(center_x, min(bottom_y, top_y + padding), 8)
        else:
            override = ass_position_override(center_x, max(top_y, bottom_y - padding), 2)
    for item in items:
        if not item.text.strip():
            continue
        event_lines.append(
            f"Dialogue: 0,{srt_time_to_ass_time(item.start)},{srt_time_to_ass_time(item.end)},Default,,0,0,0,,{override}{escape_ass_text(item.text)}"
        )
    output_ass.write_text(header + "\n".join(event_lines) + "\n", encoding="utf-8-sig")
    print(f"ASS 字幕渲染脚本: {video_width}x{video_height}, Font={font_name}, FontSize={font_size} -> {output_ass}")
    return output_ass


def seconds_to_srt_time(value: float) -> str:
    total_millis = int(round(max(0.0, float(value)) * 1000))
    hours, remainder = divmod(total_millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def srt_time_to_seconds(value: str) -> float:
    match = re.match(r"^\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*$", value)
    if not match:
        raise ValueError(f"无效的 SRT 时间: {value}")
    hours, minutes, seconds, millis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")[:3]) / 1000


def subtitle_time_intervals(items: Sequence[SubtitleItem], padding: float = 0.04, merge_gap: float = 0.08) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for item in items:
        if not item.text.strip():
            continue
        try:
            start = max(0.0, srt_time_to_seconds(item.start) - padding)
            end = max(start, srt_time_to_seconds(item.end) + padding)
        except ValueError:
            continue
        if end > start:
            intervals.append((start, end))
    intervals.sort()
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if not merged or start - merged[-1][1] > merge_gap:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


def ffmpeg_subtitle_enable_expression(items: Sequence[SubtitleItem]) -> str:
    intervals = subtitle_time_intervals(items)
    if not intervals:
        return ""
    return "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in intervals)


def adjust_srt_timing(
    input_srt: Path,
    output_srt: Path,
    trim_start: float,
    trim_end: float,
    speed: float,
    source_duration: float | None,
) -> None:
    """Adapt subtitle timestamps to the transformed video timeline.

    The pipeline extracts/translates subtitles before the video transform. If the
    transform trims the head/tail or changes playback speed, rendered subtitles
    must be shifted/scaled to match the final video.
    """
    if speed <= 0:
        raise ValueError("speed 必须大于 0")
    items = parse_srt(input_srt)
    start_cut = max(0.0, float(trim_start or 0.0))
    end_cut = max(0.0, float(trim_end or 0.0))
    if source_duration is None or source_duration <= 0:
        source_end = float("inf")
    else:
        source_end = max(start_cut, source_duration - end_cut)

    adjusted: list[SubtitleItem] = []
    for item in items:
        start = srt_time_to_seconds(item.start)
        end = srt_time_to_seconds(item.end)
        if end <= start_cut or start >= source_end:
            continue
        start = max(start, start_cut)
        end = min(end, source_end)
        new_start = (start - start_cut) / speed
        new_end = (end - start_cut) / speed
        if new_end <= new_start:
            continue
        adjusted.append(SubtitleItem(len(adjusted) + 1, seconds_to_srt_time(new_start), seconds_to_srt_time(new_end), item.text))
    write_srt(adjusted, output_srt)


def resolve_whisper_device(requested: str) -> str:
    requested = (requested or "auto").strip().lower()
    if requested == "cpu":
        return "cpu"
    preload_nvidia_runtime_dlls()
    try:
        import ctranslate2

        cuda_available = bool(ctranslate2.get_supported_compute_types("cuda"))
    except Exception:
        cuda_available = False
    if requested == "cuda" and not cuda_available:
        raise RuntimeError("Whisper 已指定 CUDA，但 CTranslate2 未检测到可用 CUDA 运行环境。")
    return "cuda" if cuda_available else "cpu"


def transcribe_video(
    video: Path,
    output_srt: Path,
    model_size: str,
    language: str,
    device: str,
    ffmpeg: str,
    word_timestamps_output: Path | None = None,
) -> None:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("未安装 faster-whisper。可执行：pip install faster-whisper") from exc
    output_srt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="subtitle-audio-") as tmp:
        started = time.perf_counter()
        stage_log(f"ASR [1/3] 提取音频: {video.name}")
        audio = Path(tmp) / "audio.wav"
        command = [ffmpeg, "-hide_banner", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)]
        run(command)
        requested_device = device
        resolved_device = resolve_whisper_device(requested_device)
        compute_type = "float16" if resolved_device == "cuda" else "int8"
        stage_log(
            f"ASR [2/3] 加载 faster-whisper: model={model_size}, requested={requested_device}, "
            f"device={resolved_device}, compute_type={compute_type}"
        )
        try:
            model = WhisperModel(model_size, device=resolved_device, compute_type=compute_type)
        except Exception as exc:
            if requested_device != "auto" or resolved_device != "cuda":
                raise RuntimeError(f"Whisper CUDA 模型加载失败: {exc}") from exc
            stage_log(f"Whisper CUDA 加载失败，自动回退 CPU: {exc}")
            resolved_device = "cpu"
            compute_type = "int8"
            model = WhisperModel(model_size, device=resolved_device, compute_type=compute_type)
        segments, info = model.transcribe(
            str(audio),
            language=None if language == "auto" else language,
            vad_filter=True,
            word_timestamps=True,
        )
        duration = max(0.0, float(getattr(info, "duration", 0.0) or 0.0))
        stage_log(f"ASR [3/3] 开始转录，语言={info.language}, probability={info.language_probability:.2f}, duration={duration:.1f}s")
        items = []
        words_payload: list[dict] = []
        last_percent = -10
        for idx, segment in enumerate(segments, 1):
            text = segment.text.strip()
            if text:
                items.append(SubtitleItem(idx, seconds_to_srt_time(segment.start), seconds_to_srt_time(segment.end), text))
            for word in getattr(segment, "words", None) or []:
                word_text = str(getattr(word, "word", ""))
                if not word_text.strip():
                    continue
                words_payload.append(
                    {
                        "text": word_text,
                        "start": round(float(getattr(word, "start", segment.start)), 3),
                        "end": round(float(getattr(word, "end", segment.end)), 3),
                        "probability": round(float(getattr(word, "probability", 0.0) or 0.0), 4),
                        "segment": idx,
                    }
                )
            if duration > 0:
                percent = min(100, int(float(segment.end) / duration * 100))
                bucket = percent // 10 * 10
                if bucket >= last_percent + 10:
                    last_percent = bucket
                    stage_log(f"ASR 进度: {percent}% ({segment.end:.1f}/{duration:.1f}s)，已生成 {len(items)} 条")
            elif idx % 20 == 0:
                stage_log(f"ASR 进度: 已生成 {len(items)} 条，当前时间 {segment.end:.1f}s")
        write_srt(items, output_srt)
        if word_timestamps_output:
            word_timestamps_output.parent.mkdir(parents=True, exist_ok=True)
            word_timestamps_output.write_text(
                json.dumps(
                    {
                        "language": info.language,
                        "language_probability": float(info.language_probability),
                        "words": words_payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            stage_log(f"ASR 单词时间戳: {len(words_payload)} 个 -> {word_timestamps_output}")
        stage_log(f"ASR 完成: {len(items)} 条，用时 {time.perf_counter() - started:.1f}s -> {output_srt}")


def chat_json_array_openai_compatible(
    *,
    prompt: str,
    user_payload: dict,
    model: str,
    expected_count: int,
    log_label: str,
) -> list[str]:
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        raise ValueError("未设置 OPENAI_API_KEY 或 LLM_API_KEY，无法自动调用大模型。")
    base_url = (os.environ.get("OPENAI_BASE_URL") or "https://theruta.ai/api/v1/chat/completions").rstrip("/")
    url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "temperature": 0.1,
    }
    request_data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    timeout_seconds = int(os.environ.get("LLM_TIMEOUT_SECONDS", "600"))
    max_attempts = int(os.environ.get("LLM_MAX_ATTEMPTS", "3"))
    print(f"{log_label}请求: model={model}, endpoint={url}, items={expected_count}")
    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        try:
            print(f"{log_label}尝试 {attempt}/{max_attempts}，超时 {timeout_seconds} 秒")
            data = fetch_chat_completion_json_with_slot(url, request_data, api_key, timeout_seconds, log_label)
            print(f"{log_label}返回，用时 {time.perf_counter() - started:.1f} 秒")
            try:
                content = data["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, TypeError, AttributeError) as exc:
                safe_preview = json.dumps(data, ensure_ascii=False)[:500]
                raise RuntimeError(f"{log_label}返回格式无效: {safe_preview}") from exc
            if not content:
                raise RuntimeError(f"{log_label}返回了空内容")
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                match = re.search(r"\[[\s\S]*\]", content)
                if not match:
                    raise RuntimeError(f"{log_label}未返回 JSON 数组: {content[:300]}")
                try:
                    result = json.loads(match.group(0))
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{log_label}返回的 JSON 数组损坏: {content[:300]}") from exc
            actual_count = len(result) if isinstance(result, list) else "非数组"
            if not isinstance(result, list) or len(result) != expected_count:
                if attempt >= max_attempts:
                    raise RuntimeError(f"{log_label}结果数量与字幕数量不一致：期望 {expected_count}，实际 {actual_count}。")
                delay = 2 ** attempt
                print(f"{log_label}数量不一致，{delay} 秒后重试 ({attempt}/{max_attempts})：期望 {expected_count}，实际 {actual_count}")
                time.sleep(delay)
                continue
            if any(not isinstance(item, str) for item in result):
                if attempt >= max_attempts:
                    raise RuntimeError(f"{log_label}必须返回仅包含字符串的 JSON 数组。")
                delay = 2 ** attempt
                print(f"{log_label}结果包含非字符串，{delay} 秒后重试 ({attempt}/{max_attempts})")
                time.sleep(delay)
                continue
            cleaned = [clean_translated_text(item) for item in result]
            print(f"{log_label}成功: items={len(cleaned)}")
            return cleaned
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in {403, 408, 409, 429, 500, 502, 503, 504}
            if not retryable or attempt >= max_attempts:
                raise RuntimeError(f"{log_label}失败: HTTP {exc.code} {body}") from exc
            delay = 2 ** attempt
            print(f"{log_label}失败，{delay} 秒后重试 ({attempt}/{max_attempts}): HTTP {exc.code}")
            time.sleep(delay)
        except urllib.error.URLError as exc:
            if attempt >= max_attempts:
                raise RuntimeError(f"{log_label}连接失败: {exc}") from exc
            delay = 2 ** attempt
            print(f"{log_label}连接失败，{delay} 秒后重试 ({attempt}/{max_attempts}): {exc}")
            time.sleep(delay)
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, ssl.SSLError) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(f"{log_label}远端连接中断: {exc}") from exc
            delay = 2 ** attempt
            print(f"{log_label}远端连接中断，{delay} 秒后重试 ({attempt}/{max_attempts}): {exc}")
            time.sleep(delay)
        except (TimeoutError, socket.timeout) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(f"{log_label}读取超时: {timeout_seconds} 秒") from exc
            delay = 2 ** attempt
            print(f"{log_label}读取超时，{delay} 秒后重试 ({attempt}/{max_attempts})")
            time.sleep(delay)
        except RuntimeError as exc:
            if attempt >= max_attempts:
                raise
            delay = 2 ** attempt
            print(f"{log_label}返回内容不可用，{delay} 秒后重试 ({attempt}/{max_attempts})：{exc}")
            time.sleep(delay)
    raise RuntimeError(f"{log_label}未返回可用结果")


def _parse_indexed_translation_content(
    content: str,
    requested_indexes: list[int],
) -> dict[int, str]:
    """Parse indexed output; accept an exact legacy string array as fallback."""
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            raise RuntimeError(f"未返回完整 JSON 对象: {content[:300]}") from exc
        try:
            result = json.loads(match.group(0))
        except json.JSONDecodeError as nested:
            raise RuntimeError(f"JSON 对象损坏: {content[:300]}") from nested

    rows = result.get("translations") if isinstance(result, dict) else result
    if not isinstance(rows, list):
        raise RuntimeError("JSON 必须包含 translations 数组。")
    if rows and all(isinstance(value, str) for value in rows):
        if len(rows) != len(requested_indexes):
            raise RuntimeError(
                f"旧数组格式数量不一致：期望 {len(requested_indexes)}，实际 {len(rows)}。"
            )
        return {
            index: clean_translated_text(text)
            for index, text in zip(requested_indexes, rows)
        }

    parsed: dict[int, str] = {}
    allowed = set(requested_indexes)
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("translations 只能包含 {index,text} 对象。")
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("translations 中存在无效 index。") from exc
        text = row.get("text")
        if not isinstance(text, str):
            raise RuntimeError(f"索引 {index} 的 text 不是字符串。")
        if index not in allowed:
            continue
        if index in parsed:
            raise RuntimeError(f"索引 {index} 重复返回。")
        parsed[index] = clean_translated_text(text)
    return parsed


def _recover_complete_indexed_translation_rows(
    content: str,
    requested_indexes: list[int],
) -> dict[int, str]:
    """Recover only fully formed row objects from a truncated translations array."""
    marker = re.search(r'"translations"\s*:\s*\[', content)
    if not marker:
        return {}
    allowed = set(requested_indexes)
    decoder = json.JSONDecoder()
    position = marker.end()
    recovered: dict[int, str] = {}
    while position < len(content):
        while position < len(content) and (content[position].isspace() or content[position] == ","):
            position += 1
        if position >= len(content) or content[position] == "]":
            break
        if content[position] != "{":
            break
        try:
            row, end_position = decoder.raw_decode(content, position)
        except json.JSONDecodeError:
            # The first object that cannot be decoded is the truncated tail.
            # Never keep a half-written subtitle or scan beyond it.
            break
        if not isinstance(row, dict):
            break
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            break
        text = row.get("text")
        if not isinstance(text, str):
            break
        if index in recovered:
            # Ambiguous duplicate rows are not safe to salvage.
            return {}
        if index in allowed:
            recovered[index] = clean_translated_text(text)
        position = end_position
    return recovered


def chat_indexed_translations_openai_compatible(
    *,
    prompt: str,
    user_payload: dict,
    model: str,
    item_indexes: list[int],
    log_label: str,
) -> list[str]:
    """Translate by stable index and refill only missing rows within this run."""
    if len(set(item_indexes)) != len(item_indexes):
        raise ValueError("初译输入 index 必须唯一。")
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        raise ValueError("未设置 OPENAI_API_KEY 或 LLM_API_KEY，无法自动调用大模型。")
    base_url = (os.environ.get("OPENAI_BASE_URL") or "https://theruta.ai/api/v1/chat/completions").rstrip("/")
    url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    timeout_seconds = int(os.environ.get("LLM_TIMEOUT_SECONDS", "600"))
    # One semantic request per recovery round by default: at most three paid
    # requests for one translation job. Operators may still opt into extra
    # transport attempts explicitly through LLM_MAX_ATTEMPTS.
    max_attempts = max(1, int(os.environ.get("LLM_MAX_ATTEMPTS", "1")))
    max_rounds = max(1, int(os.environ.get("LLM_TRANSLATION_ROUNDS", "3")))
    max_tokens = max(1024, int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "8192")))
    collected: dict[int, str] = {}
    print(f"{log_label}请求: model={model}, endpoint={url}, items={len(item_indexes)}, indexed=yes")
    last_error: Exception | None = None
    for round_number in range(1, max_rounds + 1):
        missing = [index for index in item_indexes if index not in collected]
        if not missing:
            break
        if round_number > 1:
            delay = min(30, 5 * (2 ** (round_number - 2)))
            print(
                f"{log_label}开启初译回退轮次 {round_number}/{max_rounds}，仍缺 {len(missing)} 条；"
                f"等待 {delay} 秒并将重试请求限制为全局 2 并发。"
            )
            time.sleep(delay)

        for attempt in range(1, max_attempts + 1):
            missing = [index for index in item_indexes if index not in collected]
            if not missing:
                break
            attempt_payload = dict(user_payload)
            attempt_payload.update(
                {
                    "expected_count": len(item_indexes),
                    "requested_indexes": missing,
                    "expected_return_count": len(missing),
                    "output_format": {"translations": [{"index": missing[0], "text": "translated subtitle"}]},
                }
            )
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(attempt_payload, ensure_ascii=False)},
                ],
                "temperature": 0.1,
                "max_tokens": max_tokens,
            }
            request_data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            started = time.perf_counter()
            try:
                print(
                    f"{log_label}轮次 {round_number}/{max_rounds}，尝试 {attempt}/{max_attempts}，"
                    f"待返回 {len(missing)} 条，超时 {timeout_seconds} 秒"
                )
                retry_slot_limit = 2 if round_number > 1 else None
                data = fetch_chat_completion_json_with_slot(
                    url, request_data, api_key, timeout_seconds, log_label, retry_slot_limit
                )
                print(f"{log_label}返回，用时 {time.perf_counter() - started:.1f} 秒")
                try:
                    content = data["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, TypeError, AttributeError) as exc:
                    safe_preview = json.dumps(data, ensure_ascii=False)[:500]
                    raise RuntimeError(f"返回格式无效: {safe_preview}") from exc
                if not content:
                    raise RuntimeError("返回了空内容")
                try:
                    parsed = _parse_indexed_translation_content(content, missing)
                except RuntimeError as exc:
                    recovered = _recover_complete_indexed_translation_rows(content, missing)
                    if not recovered:
                        raise
                    parsed = recovered
                    print(
                        f"{log_label}响应 JSON 截断，已安全抢救 {len(recovered)} 条完整索引；"
                        "截断中的半条字幕已丢弃。"
                    )
                    last_error = exc
                collected.update(parsed)
                remaining = [index for index in item_indexes if index not in collected]
                if remaining:
                    print(
                        f"{log_label}本轮收到 {len(parsed)} 条，仍缺 {len(remaining)} 条；"
                        f"将携带完整源上下文只补发缺失索引: {remaining[:20]}{'...' if len(remaining) > 20 else ''}"
                    )
                else:
                    print(f"{log_label}成功: items={len(collected)}, indexed=yes")
                    break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code in {403, 408, 409, 429, 500, 502, 503, 504}
                if not retryable:
                    raise RuntimeError(f"{log_label}失败: HTTP {exc.code} {body}") from exc
                last_error = RuntimeError(f"HTTP {exc.code} {body}")
                print(f"{log_label}HTTP {exc.code}，准备重试。")
            except urllib.error.URLError as exc:
                last_error = exc
                print(f"{log_label}连接失败，准备重试: {exc}")
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, ssl.SSLError) as exc:
                last_error = exc
                print(f"{log_label}远端连接中断，准备重试: {exc}")
            except (TimeoutError, socket.timeout) as exc:
                last_error = exc
                print(f"{log_label}读取超时，准备重试。")
            except (RuntimeError, json.JSONDecodeError) as exc:
                last_error = exc
                print(
                    f"{log_label}返回内容不可用，准备重试 "
                    f"(轮次 {round_number}/{max_rounds}，尝试 {attempt}/{max_attempts}): {exc}"
                )
            if attempt < max_attempts:
                time.sleep(2 ** attempt)

        if collected and len(collected) < len(item_indexes):
            print(f"{log_label}当前任务内已累计保存 {len(collected)}/{len(item_indexes)} 条完整译文。")

    missing = [index for index in item_indexes if index not in collected]
    if missing:
        raise RuntimeError(
            f"{log_label}经过 {max_rounds} 轮初译后仍缺少 {len(missing)} 条字幕，索引: "
            f"{missing[:30]}{'...' if len(missing) > 30 else ''}；最后错误: {last_error}"
        )
    return [collected[index] for index in item_indexes]


def chat_json_object_openai_compatible(
    *,
    prompt: str,
    user_payload: dict,
    model: str,
    expected_count: int,
    log_label: str,
    request_count: int | None = None,
) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        raise ValueError("未设置 OPENAI_API_KEY 或 LLM_API_KEY，无法自动调用大模型。")
    base_url = (os.environ.get("OPENAI_BASE_URL") or "https://theruta.ai/api/v1/chat/completions").rstrip("/")
    url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "temperature": 0.1,
    }
    request_data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    timeout_seconds = int(os.environ.get("LLM_TIMEOUT_SECONDS", "600"))
    max_attempts = int(os.environ.get("LLM_MAX_ATTEMPTS", "3"))
    print(f"{log_label}请求: model={model}, endpoint={url}, items={request_count if request_count is not None else expected_count}")
    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        try:
            print(f"{log_label}尝试 {attempt}/{max_attempts}，超时 {timeout_seconds} 秒")
            data = fetch_chat_completion_json_with_slot(url, request_data, api_key, timeout_seconds, log_label)
            print(f"{log_label}返回，用时 {time.perf_counter() - started:.1f} 秒")
            try:
                content = data["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, TypeError, AttributeError) as exc:
                safe_preview = json.dumps(data, ensure_ascii=False)[:500]
                raise RuntimeError(f"{log_label}返回格式无效: {safe_preview}") from exc
            if not content:
                raise RuntimeError(f"{log_label}返回了空内容")
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                match = re.search(r"\{[\s\S]*\}", content)
                if not match:
                    raise RuntimeError(f"{log_label}未返回 JSON 对象: {content[:300]}")
                try:
                    result = json.loads(match.group(0))
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{log_label}返回的 JSON 对象损坏: {content[:300]}") from exc
            if not isinstance(result, dict):
                raise RuntimeError(f"{log_label}必须返回 JSON 对象。")
            subtitles = result.get("subtitles")
            actual_count = len(subtitles) if isinstance(subtitles, list) else "非数组"
            if not isinstance(subtitles, list) or len(subtitles) != expected_count:
                if attempt >= max_attempts:
                    raise RuntimeError(f"{log_label}字幕数量不一致：期望 {expected_count}，实际 {actual_count}。")
                delay = 2 ** attempt
                print(f"{log_label}字幕数量不一致，{delay} 秒后重试 ({attempt}/{max_attempts})：期望 {expected_count}，实际 {actual_count}")
                time.sleep(delay)
                continue
            if any(not isinstance(item, str) for item in subtitles):
                if attempt >= max_attempts:
                    raise RuntimeError(f"{log_label}的 subtitles 必须只包含字符串。")
                delay = 2 ** attempt
                print(f"{log_label}字幕包含非字符串，{delay} 秒后重试 ({attempt}/{max_attempts})")
                time.sleep(delay)
                continue
            result["subtitles"] = [clean_translated_text(item) for item in subtitles]
            if not isinstance(result.get("review"), dict):
                result["review"] = {"summary": str(result.get("review", "")).strip()}
            print(f"{log_label}成功: items={len(result['subtitles'])}")
            return result
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in {403, 408, 409, 429, 500, 502, 503, 504}
            if not retryable or attempt >= max_attempts:
                raise RuntimeError(f"{log_label}失败: HTTP {exc.code} {body}") from exc
            delay = 2 ** attempt
            print(f"{log_label}失败，{delay} 秒后重试 ({attempt}/{max_attempts}): HTTP {exc.code}")
            time.sleep(delay)
        except urllib.error.URLError as exc:
            if attempt >= max_attempts:
                raise RuntimeError(f"{log_label}连接失败: {exc}") from exc
            delay = 2 ** attempt
            print(f"{log_label}连接失败，{delay} 秒后重试 ({attempt}/{max_attempts}): {exc}")
            time.sleep(delay)
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, ssl.SSLError) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(f"{log_label}远端连接中断: {exc}") from exc
            delay = 2 ** attempt
            print(f"{log_label}远端连接中断，{delay} 秒后重试 ({attempt}/{max_attempts}): {exc}")
            time.sleep(delay)
        except (TimeoutError, socket.timeout) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(f"{log_label}读取超时: {timeout_seconds} 秒") from exc
            delay = 2 ** attempt
            print(f"{log_label}读取超时，{delay} 秒后重试 ({attempt}/{max_attempts})")
            time.sleep(delay)
        except RuntimeError as exc:
            if attempt >= max_attempts:
                raise
            delay = 2 ** attempt
            print(f"{log_label}返回内容不可用，{delay} 秒后重试 ({attempt}/{max_attempts})：{exc}")
            time.sleep(delay)
    raise RuntimeError(f"{log_label}未返回可用结果")


def review_translations_openai_compatible(
    source_texts: list[str],
    target_language: str,
    source_language: str,
    model: str,
    translation_a: list[str],
    translation_b: list[str],
    source_kind: str = "ocr",
) -> list[str]:
    if len(source_texts) != len(translation_a) or len(source_texts) != len(translation_b):
        raise RuntimeError("审核输入数量不一致，无法交叉审核字幕。")
    if source_kind == "asr":
        source_description = (
            "The source items come from ASR speech recognition. They are usually Chinese or English dialogue and are mostly reliable, "
            "but may contain minor punctuation, casing, homophone, or segmentation mistakes. Do light correction only. "
            "Do not delete standalone numbers unless they are clearly not speech."
        )
    elif source_kind == "soft":
        source_description = (
            "The source items come from an embedded subtitle track and should be treated as reliable text. "
            "Do only minimal cleanup before translating."
        )
    else:
        source_description = (
            "The source items come from hard-subtitle OCR and may contain recognition noise, broken word order, "
            "lone digits/symbols, UI leftovers, duplicated fragments, and unnatural broken wording."
        )
    prompt = (
        "You are the final reviewer for short-drama subtitle translation. "
        "You receive source subtitle items and two independent translations. "
        f"{source_description} "
        "Choose or rewrite the final subtitle for each item in the target language. "
        "Use the source text, neighboring context, and both translations to remove only source-appropriate noise and unnatural wording. "
        "Prefer fluent, concise subtitles that fit a short video. "
        "When the source is English, detect idioms, phrasal verbs, slang, euphemisms, sarcasm, and other figurative expressions. "
        "Translate their intended contextual meaning into natural target-language speech instead of preserving literal English imagery or word order. "
        "Treat a word-for-word rendering of an English idiom as a translation error and rewrite it, without overinterpreting ordinary literal statements. "
        f"{TERM_CONSISTENCY_GUIDANCE} "
        "Do NOT invent unsupported plot, names, facts, emotions, or dialogue. "
        "If both translations are bad, infer the safest natural subtitle from the source and context; if unrecoverable, return an empty string. "
        "The return must have two parts: a review report and the final subtitle array. "
        "In review, state which side has bigger problems: source/OCR-ASR, translation_a, translation_b, both translations, or none. "
        "Keep the report short but concrete; mention 1-5 representative issue examples by index when useful. "
        "Do not add explanations, notes, timestamps, numbering, or extra text inside subtitles. "
        f"Keep the output count exactly {len(source_texts)} and keep the same order. "
        "Return JSON only with this shape: "
        '{"review":{"bigger_problem":"source|translation_a|translation_b|both_translations|none","summary":"...","examples":[{"index":1,"issue":"..."}]},"subtitles":["..."]}.'
    )
    result = chat_json_object_openai_compatible(
        prompt=prompt,
        user_payload={
            "task": "review_two_subtitle_translations",
            "source_language": source_language,
            "target_language": target_language,
            "expected_count": len(source_texts),
            "source_kind": source_kind,
            "source_items": [clean_text_for_translation(text, source_kind) for text in source_texts],
            "translation_a": translation_a,
            "translation_b": translation_b,
        },
        model=model,
        expected_count=len(source_texts),
        log_label="AI 审核",
    )
    review = result.get("review", {})
    print("AI 审核结果:")
    print(json.dumps(review, ensure_ascii=False))
    return result["subtitles"]


def review_single_translation_openai_compatible(
    source_texts: list[str],
    target_language: str,
    source_language: str,
    model: str,
    initial_translation: list[str],
    source_kind: str = "ocr",
    glossary_prompt: str = "",
    diagnostics: dict | None = None,
    timed_items: list[SubtitleItem] | None = None,
) -> list[str]:
    if len(source_texts) != len(initial_translation):
        raise RuntimeError("单源审核输入数量不一致。")
    if timed_items is None:
        timed_items = [
            SubtitleItem(index + 1, seconds_to_srt_time(float(index)), seconds_to_srt_time(float(index + 1)), text)
            for index, text in enumerate(source_texts)
        ]
    if len(timed_items) != len(source_texts):
        raise RuntimeError("单源审核时间轴数量不一致。")
    prompt = (
        "You are the final reviewer for short-drama subtitle translation. You receive one source subtitle track and one initial translation. "
        f"The source kind is {source_kind}. Correct only clear recognition, OCR, or target-language errors. "
        "For English source text, explicitly check idioms, phrasal verbs, slang, euphemisms, sarcasm, and figurative language. "
        "Translate the intended meaning naturally; a literal word-for-word rendering of an idiom is an error that must be corrected. "
        f"{TERM_CONSISTENCY_GUIDANCE} "
        "Review the complete episode as a continuous scene, not as unrelated rows. "
        "Return sparse edits only for rows that genuinely need correction. Use replace for one row, merge for 2-8 contiguous snapshots of one utterance, "
        "and delete only for unmistakable debris. Omit correct rows. Never delete more than a small minority of the episode, merge more than 6 seconds, "
        "move meaning across non-contiguous indexes, or invent unsupported content. The local program independently rejects excessive edits. "
        "In the review report, include an entities array for recurring people, families, places, organizations, ranks, and titles found in this episode. "
        "Do not group distinct people merely because they share a family, title, role, or scene. Include evidence_indexes and confidence for each hypothesis. "
        "Return JSON only with an empty subtitles array and this shape: "
        '{"review":{"bigger_problem":"source|initial_translation|both|none","summary":"...","examples":[],"entities":[{"kind":"person|family|place|organization|rank|title","source_aliases":["..."],"target_variants":["..."],"preferred_target":"...","evidence_indexes":[1],"confidence":0.9}]},'
        '"edits":[{"action":"replace|merge|delete","indexes":[1],"text":"...","reason":"..."}],"subtitles":[]}.'
    )
    prompt += glossary_prompt
    result = chat_json_object_openai_compatible(
        prompt=prompt,
        user_payload={
            "task": "review_single_subtitle_translation",
            "source_language": source_language,
            "target_language": target_language,
            "source_kind": source_kind,
            "expected_count": 0,
            "items": [
                {
                    "index": timed_items[index].index,
                    "start": timed_items[index].start,
                    "end": timed_items[index].end,
                    "source": clean_text_for_translation(text, source_kind),
                    "initial_translation": initial_translation[index],
                }
                for index, text in enumerate(source_texts)
            ],
        },
        model=model,
        expected_count=0,
        log_label="AI 单源字幕审核",
        request_count=len(source_texts),
    )
    print("AI 单源字幕审核结果:")
    print(json.dumps(result.get("review", {}), ensure_ascii=False))
    if diagnostics is not None:
        diagnostics["review_report"] = result.get("review", {})
    edits = result.get("edits") if isinstance(result.get("edits"), list) else []
    final, edit_stats = apply_episode_review_edits(timed_items, initial_translation, edits)
    print(f"AI 单源字幕审核编辑应用: {json.dumps(edit_stats, ensure_ascii=False)}")
    if edit_stats.get("safety_rejected"):
        print("AI 单源字幕审核有操作触发本地安全熔断，已保留对应初译。", file=sys.stderr)
    if diagnostics is not None:
        diagnostics["edit_stats"] = edit_stats
    return final


def translate_texts_with_optional_review(
    texts: list[str],
    target_language: str,
    source_language: str,
    model: str,
    enable_review: bool = False,
    model_b: str = "",
    review_model: str = "",
    source_kind: str = "ocr",
    diagnostics: dict | None = None,
    glossary_prompt: str = "",
    timed_items: list[SubtitleItem] | None = None,
) -> list[str]:
    translation_a = translate_texts_openai_compatible(
        texts, target_language, source_language, model, source_kind, glossary_prompt
    )
    if diagnostics is not None:
        diagnostics["initial_translation"] = list(translation_a)
    if not enable_review:
        if diagnostics is not None:
            diagnostics["final_translation"] = list(translation_a)
        return translation_a
    review_model = review_model.strip() or model
    print(f"AI 单源二次审核开启：初译={model}，审核={review_model}")
    try:
        final = review_single_translation_openai_compatible(
            texts, target_language, source_language, review_model, translation_a, source_kind,
            glossary_prompt, diagnostics, timed_items,
        )
        if diagnostics is not None:
            diagnostics["review"] = {"model": review_model, "status": "completed"}
            diagnostics["final_translation"] = list(final)
        return final
    except Exception as exc:
        print(f"单源字幕审核失败，自动使用 Flash 初译继续: {exc}", file=sys.stderr)
        if diagnostics is not None:
            diagnostics["review"] = {"model": review_model, "status": "failed", "error": str(exc)}
            diagnostics["final_translation"] = list(translation_a)
        return translation_a


def translate_dual_source_texts_openai_compatible(
    pairs: list[AlignedSubtitlePair],
    target_language: str,
    visual_language: str,
    audio_language: str,
    model: str,
    visual_kind: str = "ocr",
    glossary_prompt: str = "",
) -> list[str]:
    prompt = (
        "You are a professional short-drama subtitle translator using two synchronized evidence sources. "
        f"The visual source is {visual_kind} text in {visual_language}; it may contain OCR noise when the kind is OCR. "
        f"The audio source is ASR speech recognition in {audio_language}; it may contain homophone, punctuation, or segmentation errors. "
        "The two sources describe the same timeline and may use different languages. Compare both sources for every item, "
        "repair only obvious recognition errors, and output one natural concise subtitle in the target language. "
        "When either source is English, recognize idioms, phrasal verbs, slang, euphemisms, sarcasm, and figurative expressions. "
        "Translate the intended contextual meaning, not the literal image or English word order, using idiomatic native phrasing in the target language. "
        "For example, 'fall right into my lap' means receiving something unexpectedly or with little effort; it does not describe a physical fall. "
        "Do not overinterpret an ordinary sentence that is being used literally. "
        f"{TERM_CONSISTENCY_GUIDANCE} "
        "The visual item defines the current subtitle boundary; ASR word timestamps are supporting evidence only. "
        "Never move words that belong to neighboring subtitle indexes into the current item. "
        "Do not concatenate two alternative readings. Prefer the source that is clearer and more contextually consistent. "
        "An empty source means that source missed the item; never invent text merely to fill an empty field. "
        "Do not add explanations, timestamps, numbering, names, plot, or unsupported dialogue. "
        "The user payload contains requested_indexes. Return exactly one object for every requested index, even when its translation is empty. "
        "Never merge, renumber, omit, or invent indexes. Return JSON only in this shape: "
        '{"translations":[{"index":1,"text":"translated subtitle"}]}.'
    )
    prompt += glossary_prompt
    payload_items = [
        {
            "index": pair.index,
            "start": pair.start,
            "end": pair.end,
            "visual_source": clean_text_for_translation(pair.visual_text, visual_kind),
            "audio_asr_source": clean_text_for_translation(pair.audio_text, "asr"),
            "asr_word_confidence": round(pair.audio_confidence, 3),
            "temporal_confidence": round(pair.temporal_confidence, 3),
            "combined_confidence": round(pair.confidence_score, 3),
        }
        for pair in pairs
    ]
    return chat_indexed_translations_openai_compatible(
        prompt=prompt,
        user_payload={
            "task": "translate_aligned_ocr_asr_subtitles",
            "target_language": target_language,
            "visual_source_language": visual_language,
            "audio_source_language": audio_language,
            "visual_source_kind": visual_kind,
            "items": payload_items,
        },
        model=model,
        item_indexes=[pair.index for pair in pairs],
        log_label="AI 双源翻译",
    )


def review_dual_source_translations_openai_compatible(
    pairs: list[AlignedSubtitlePair],
    target_language: str,
    visual_language: str,
    audio_language: str,
    model: str,
    translation_a: list[str],
    translation_b: list[str],
    visual_kind: str = "ocr",
) -> list[str]:
    if len(pairs) != len(translation_a) or len(pairs) != len(translation_b):
        raise RuntimeError("双源审核输入数量不一致。")
    prompt = (
        "You are the final reviewer for synchronized short-drama subtitles. Each item contains visual OCR/soft-subtitle evidence, "
        "audio ASR evidence, and two independent candidate translations. The sources can be in different languages but refer to the same time. "
        "Compare meaning and neighboring context, identify recognition or translation errors, and return one safe fluent final subtitle per item. "
        "Do not invent unsupported dialogue. If one source is empty, use the other; if both are unrecoverable, return an empty string. "
        "Return a short review report that distinguishes visual-source, audio-ASR, translation-A, and translation-B problems, plus final subtitles. "
        f"Keep exactly {len(pairs)} subtitles in the same order. Return JSON only with this shape: "
        '{"review":{"bigger_problem":"visual_source|audio_asr|translation_a|translation_b|both_translations|none","summary":"...","examples":[{"index":1,"issue":"..."}]},"subtitles":["..."]}.'
    )
    result = chat_json_object_openai_compatible(
        prompt=prompt,
        user_payload={
            "task": "review_aligned_ocr_asr_translations",
            "target_language": target_language,
            "visual_source_language": visual_language,
            "audio_source_language": audio_language,
            "visual_source_kind": visual_kind,
            "expected_count": len(pairs),
            "items": [
                {
                    "index": pair.index,
                    "start": pair.start,
                    "end": pair.end,
                    "visual_source": clean_text_for_translation(pair.visual_text, visual_kind),
                    "audio_asr_source": clean_text_for_translation(pair.audio_text, "asr"),
                    "translation_a": translation_a[index],
                    "translation_b": translation_b[index],
                }
                for index, pair in enumerate(pairs)
            ],
        },
        model=model,
        expected_count=len(pairs),
        log_label="AI 双源审核",
    )
    print("AI 双源审核结果:")
    print(json.dumps(result.get("review", {}), ensure_ascii=False))
    return result["subtitles"]


def review_risky_dual_source_translations_openai_compatible(
    pairs: list[AlignedSubtitlePair],
    target_language: str,
    visual_language: str,
    audio_language: str,
    model: str,
    initial_translation: list[str],
    confidence_threshold: float,
    visual_kind: str = "ocr",
    diagnostics: dict | None = None,
    glossary_prompt: str = "",
) -> list[str]:
    if len(pairs) != len(initial_translation):
        raise RuntimeError("智能审核输入数量不一致。")
    risk_indexes = [index for index, pair in enumerate(pairs) if pair.confidence_score < confidence_threshold]
    # Confidence remains useful as evidence, but it must not decide whether a
    # line is allowed to receive semantic review. Names, idioms and plausible
    # OCR mistakes can all score highly while still being wrong. Review the
    # complete episode so the model can make one coherent decision.
    review_indexes = list(range(len(pairs)))
    if diagnostics is not None:
        diagnostics.update(
            {
                "model": model,
                "status": "pending",
                "item_indexes": [pairs[index].index for index in review_indexes],
                "risk_item_indexes": [pairs[index].index for index in risk_indexes],
                "report": None,
            }
        )
    high_count = len(pairs) - len(risk_indexes)
    print(
        f"字幕置信度评估: high={high_count}, risk={len(risk_indexes)}, "
        f"full_episode_review={len(review_indexes)}, threshold={confidence_threshold:.2f}"
    )
    prompt = (
        "You are the final reviewer for one complete synchronized short-drama episode. Every subtitle is sent so that high-confidence "
        "translation errors and recurring-name inconsistencies cannot bypass review. "
        "Each item contains visual OCR/soft-subtitle evidence, word-timestamp-aligned audio ASR evidence, a confidence score, "
        "and one initial translation. Read the ordered list as continuous episode context. "
        f"The selected target language is {target_language}. Every non-empty edits[].text value must be written only in "
        f"{target_language}; never copy source-language text into edits[].text unless the source language is also {target_language}. "
        "The review summary and reasons may use English. "
        "Correct recognition mistakes and make the target-language subtitle natural, concise, and faithful. "
        "Explicitly audit English idioms, phrasal verbs, slang, euphemisms, sarcasm, and figurative expressions. "
        "A word-for-word translation that preserves irrelevant English imagery is a translation error: replace it with the intended contextual meaning in natural target-language speech. "
        "For example, 'fall right into my lap' means receiving something unexpectedly or easily, not physically falling into a lap. "
        f"{TERM_CONSISTENCY_GUIDANCE} "
        "Return only sparse edit operations for subtitles that genuinely need correction. Omit correct subtitles from edits. "
        "Use replace for one index, merge for 2-8 contiguous duplicate snapshots or fragments of one utterance, and delete only genuine OCR/ASR debris. "
        "A merge text becomes the identical complete subtitle across that contiguous group and is later merged over the union of original times. "
        "Never merge more than 6 seconds, delete more than a small minority of the episode, combine non-contiguous dialogue, change timing, or invent content. "
        "The local program independently rejects excessive edits. "
        "The report must also include recurring entities discovered from the evidence: people, families, places, organizations, ranks, and titles. "
        "Do not group distinct characters merely because they share a family, title, role, or scene. Every entity hypothesis needs evidence_indexes and confidence. "
        "Return JSON only with this shape: "
        '{"review":{"bigger_problem":"visual_source|audio_asr|initial_translation|multiple|none","summary":"...",'
        '"examples":[{"index":1,"issue":"..."}],"entities":[{"kind":"person|family|place|organization|rank|title",'
        '"source_aliases":["..."],"target_variants":["..."],"preferred_target":"...","evidence_indexes":[1],"confidence":0.9}]},'
        '"edits":[{"action":"replace|merge|delete","indexes":[1],"text":"...","reason":"..."}],"subtitles":[]}.'
    )
    prompt += glossary_prompt
    review_items = []
    for index in review_indexes:
        pair = pairs[index]
        review_items.append(
            {
                "index": pair.index,
                "start": pair.start,
                "end": pair.end,
                "visual_source": clean_text_for_translation(pair.visual_text, visual_kind),
                "audio_asr_source": clean_text_for_translation(pair.audio_text, "asr"),
                "asr_word_confidence": round(pair.audio_confidence, 3),
                "temporal_confidence": round(pair.temporal_confidence, 3),
                "combined_confidence": round(pair.confidence_score, 3),
                "confidence_reason": pair.confidence_reason,
                "initial_translation": initial_translation[index],
            }
        )
    result = chat_json_object_openai_compatible(
        prompt=prompt,
        user_payload={
            "task": "review_complete_aligned_episode",
            "target_language": target_language,
            "visual_source_language": visual_language,
            "audio_source_language": audio_language,
            "visual_source_kind": visual_kind,
            "confidence_threshold": confidence_threshold,
            "expected_count": 0,
            "items": review_items,
        },
        model=model,
        expected_count=0,
        log_label="AI 整集语义审核",
        request_count=len(review_items),
    )
    print("AI 整集语义审核结果:")
    print(json.dumps(result.get("review", {}), ensure_ascii=False))
    if diagnostics is not None:
        edits = result.get("edits") if isinstance(result.get("edits"), list) else []
        final, edit_stats = apply_episode_review_edits(pairs, initial_translation, edits)
        print(f"AI 整集语义审核编辑应用: {json.dumps(edit_stats, ensure_ascii=False)}")
        if edit_stats.get("safety_rejected"):
            print("AI 整集语义审核有操作触发本地安全熔断，已保留对应初译。", file=sys.stderr)
        diagnostics.update(
            {
                "model": model,
                "status": "completed",
                "item_indexes": [pairs[index].index for index in review_indexes],
                "risk_item_indexes": [pairs[index].index for index in risk_indexes],
                "report": result.get("review", {}),
                "edit_stats": edit_stats,
            }
        )
    else:
        edits = result.get("edits") if isinstance(result.get("edits"), list) else []
        final, _edit_stats = apply_episode_review_edits(pairs, initial_translation, edits)
        print(f"AI 整集语义审核编辑应用: {json.dumps(_edit_stats, ensure_ascii=False)}")
    return final


def translate_dual_source_srts(
    visual_srt: Path,
    audio_srt: Path,
    output_srt: Path,
    target_language: str,
    visual_language: str,
    audio_language: str,
    model: str,
    enable_review: bool = False,
    model_b: str = "",
    review_model: str = "",
    visual_kind: str = "ocr",
    audio_words_path: Path | None = None,
    confidence_threshold: float = 0.82,
    translation_record_path: Path | None = None,
    record_context: dict | None = None,
    glossary: dict | None = None,
) -> None:
    pairs = align_visual_and_audio_subtitles(
        parse_srt(visual_srt),
        parse_srt(audio_srt),
        audio_words=load_asr_words(audio_words_path),
    )
    if not pairs:
        raise ValueError("OCR/软字幕与 ASR 都没有可对齐的字幕。")
    for pair in pairs:
        score_aligned_pair(pair, visual_language, audio_language, visual_kind)
    glossary_prompt = build_glossary_prompt(glossary)
    record = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "aligned",
        "video": dict(record_context or {}),
        "pipeline": {
            "mode": "dual_source",
            "visual_kind": visual_kind,
            "visual_language": visual_language,
            "audio_language": audio_language,
            "target_language": target_language,
            "initial_model": model,
            "review_enabled": enable_review,
            "review_model": review_model.strip() or model,
            "confidence_threshold": confidence_threshold,
            "word_timestamps": bool(audio_words_path and audio_words_path.is_file()),
            "glossary": (
                {"id": glossary.get("id"), "name": glossary.get("name"), "source": glossary.get("_source_path")}
                if glossary else None
            ),
        },
        "items": [
            {
                "index": pair.index,
                "start": pair.start,
                "end": pair.end,
                "visual_source_raw": pair.visual_text,
                "visual_source_clean": clean_text_for_translation(pair.visual_text, visual_kind),
                "audio_asr_raw": pair.audio_text,
                "audio_asr_clean": clean_text_for_translation(pair.audio_text, "asr"),
                "asr_word_confidence": round(pair.audio_confidence, 4),
                "temporal_confidence": round(pair.temporal_confidence, 4),
                "combined_confidence": round(pair.confidence_score, 4),
                "confidence_reason": pair.confidence_reason,
                "needs_review": bool(enable_review and pair.confidence_score < confidence_threshold),
                "initial_translation": None,
                "reviewed_translation": None,
                "final_translation": None,
            }
            for pair in pairs
        ],
        "reviews": [],
        "error": None,
    }
    write_translation_record(translation_record_path, record)
    print(
        f"双源时间轴对齐完成: items={len(pairs)}, "
        f"word_timestamps={'yes' if audio_words_path and audio_words_path.is_file() else 'no'}"
    )
    translated: list[str] = []
    batch_size = 500
    total_batches = (len(pairs) + batch_size - 1) // batch_size
    try:
        for start in range(0, len(pairs), batch_size):
            current = pairs[start:start + batch_size]
            if total_batches > 1:
                print(f"双源字幕翻译 {start + 1}-{start + len(current)} / {len(pairs)}")
            translation_a = translate_dual_source_texts_openai_compatible(
                current, target_language, visual_language, audio_language, model, visual_kind, glossary_prompt
            )
            for offset, text in enumerate(translation_a):
                record["items"][start + offset]["initial_translation"] = text
            record["status"] = "initial_translated"
            write_translation_record(translation_record_path, record)
            review_diagnostics: dict = {}
            if enable_review:
                try:
                    final = review_risky_dual_source_translations_openai_compatible(
                        current,
                        target_language,
                        visual_language,
                        audio_language,
                        review_model.strip() or model,
                        translation_a,
                        confidence_threshold,
                        visual_kind,
                        review_diagnostics,
                        glossary_prompt,
                    )
                except Exception as exc:
                    print(f"低置信字幕审核失败，自动使用 Flash 初译继续: {exc}", file=sys.stderr)
                    review_diagnostics.update(
                        {"model": review_model.strip() or model, "status": "failed", "error": str(exc)}
                    )
                    final = translation_a
                record["reviews"].append(review_diagnostics)
            else:
                final = translation_a
            for offset, text in enumerate(final):
                item = record["items"][start + offset]
                item["final_translation"] = text
                if review_diagnostics.get("status") == "completed":
                    item["reviewed_translation"] = text
            translated.extend(final)
            write_translation_record(translation_record_path, record)
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        write_translation_record(translation_record_path, record)
        raise
    raw_output_items = [
        SubtitleItem(index, pair.start, pair.end, text)
        for index, (pair, text) in enumerate(zip(pairs, translated), 1)
    ]
    output_items, cleanup = clean_and_merge_output_subtitles(raw_output_items)
    write_srt(output_items, output_srt)
    record["output_cleanup"] = cleanup
    record["status"] = "completed"
    write_translation_record(translation_record_path, record)
    print(
        f"字幕终稿清理: empty={cleanup['removed_empty']}, "
        f"merged={cleanup['merged_adjacent_duplicates']}, output={cleanup['output_items']}"
    )
    if translation_record_path is not None:
        print(f"翻译诊断记录已保存: {translation_record_path}")


def translate_texts_openai_compatible(
    texts: list[str],
    target_language: str,
    source_language: str,
    model: str,
    source_kind: str = "ocr",
    glossary_prompt: str = "",
) -> list[str]:
    if source_kind == "asr":
        prompt = (
            "You are a professional short-drama ASR subtitle translator. "
            "Each input item is speech-recognition text, usually Chinese or English dialogue, and is mostly reliable. "
            "Translate each item to the target language naturally and concisely. "
            "You MAY fix minor ASR punctuation, casing, segmentation, or obvious homophone mistakes, but keep the spoken meaning. "
            "Do NOT delete standalone numbers unless they are clearly not speech. "
        )
    elif source_kind == "soft":
        prompt = (
            "You are a professional short-drama subtitle translator. "
            "Each input item comes from an embedded subtitle track and should be treated as reliable text. "
            "Translate each item to the target language naturally and concisely with only minimal cleanup. "
        )
    else:
        prompt = (
            "You are a professional short-drama OCR subtitle cleaner and translator. "
            "Each input item is OCR text from hard subtitles and may contain recognition noise, broken word order, "
            "duplicate fragments, stray single digits, random symbols, or unnatural line-break artifacts. "
            "First infer the most likely intended subtitle from context, then translate it to the target language naturally and concisely. "
            "You MAY fix obvious OCR mistakes, remove obvious garbage/noise, smooth awkward OCR wording, and repair broken fragments. "
            "Examples of removable noise include isolated digits like 5 or 9, lone punctuation, UI leftovers, repeated partial words, and fragments that clearly are not dialogue. "
        )
    prompt += (
        "When the source is English, detect idioms, phrasal verbs, slang, euphemisms, sarcasm, and figurative expressions. "
        "Translate their intended contextual meaning into idiomatic native target-language speech instead of translating word by word or preserving irrelevant English imagery. "
        "For example, 'fall right into my lap' means receiving something unexpectedly or with little effort, not physically falling into a lap. "
        "Do not overinterpret ordinary literal statements. "
        f"{TERM_CONSISTENCY_GUIDANCE} "
        "Do NOT invent new plot, names, emotions, facts, or dialogue that is not supported by the item/context. "
        "If the source is too noisy to recover, output the shortest plausible natural subtitle or an empty string. "
        "Preserve the original meaning, speaker emotion, tone, and punctuation style when recoverable. "
        "The input has already normalized subtitle line breaks into spaces; do not create artificial line breaks. "
        "Do not add explanations, notes, timestamps, numbering, or extra text. "
        "The user payload contains requested_indexes. Return exactly one object for every requested index, even when its translation is empty. "
        "Never merge, renumber, omit, or invent indexes. Return JSON only in this shape: "
        '{"translations":[{"index":1,"text":"translated subtitle"}]}.'
    )
    prompt += glossary_prompt
    cleaned_texts = [clean_text_for_translation(text, source_kind) for text in texts]
    indexed_items = [
        {"index": index, "source": text}
        for index, text in enumerate(cleaned_texts, 1)
    ]
    return chat_indexed_translations_openai_compatible(
        prompt=prompt,
        user_payload={
            "task": "translate_subtitles",
            "source_kind": source_kind,
            "source_language": source_language,
            "target_language": target_language,
            "items": indexed_items,
        },
        model=model,
        item_indexes=[item["index"] for item in indexed_items],
        log_label="AI 翻译",
    )


def translate_srt(
    input_srt: Path,
    output_srt: Path,
    target_language: str,
    source_language: str,
    provider: str,
    model: str,
    parallel_batches: int,
    enable_review: bool = False,
    model_b: str = "",
    review_model: str = "",
    source_kind: str = "ocr",
    translation_record_path: Path | None = None,
    record_context: dict | None = None,
    glossary: dict | None = None,
) -> None:
    items = parse_srt(input_srt)
    if not items:
        raise ValueError(f"未解析到字幕: {input_srt}")
    if provider == "none":
        write_srt(items, output_srt)
        print("provider=none：已复制字幕文件。你可以手动编辑后再烧录/替换。")
        return
    if provider != "openai-compatible":
        raise ValueError("provider 目前支持 none 或 openai-compatible")
    texts = [item.text for item in items]
    glossary_prompt = build_glossary_prompt(glossary)
    record = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "source_loaded",
        "video": dict(record_context or {}),
        "pipeline": {
            "mode": "single_source",
            "source_kind": source_kind,
            "source_language": source_language,
            "target_language": target_language,
            "initial_model": model,
            "review_enabled": enable_review,
            "review_model": review_model.strip() or model,
            "glossary": (
                {"id": glossary.get("id"), "name": glossary.get("name"), "source": glossary.get("_source_path")}
                if glossary else None
            ),
        },
        "items": [
            {"index": item.index, "start": item.start, "end": item.end, "source_raw": item.text,
             "source_clean": clean_text_for_translation(item.text, source_kind),
             "initial_translation": None, "final_translation": None}
            for item in items
        ],
        "batches": [],
        "error": None,
    }
    write_translation_record(translation_record_path, record)
    batch_size = 500
    translated = []
    try:
        if len(texts) <= batch_size:
            print(f"整段发送字幕: items={len(texts)}（每个视频仅 1 个 AI 请求）")
        else:
            total_batches = (len(texts) + batch_size - 1) // batch_size
            print(f"字幕超过 {batch_size} 条，按每 {batch_size} 条分批发送: items={len(texts)}, batches={total_batches}")
        for start in range(0, len(texts), batch_size):
            end = min(start + batch_size, len(texts))
            if len(texts) > batch_size:
                print(f"翻译字幕 {start + 1}-{end} / {len(texts)}")
            batch_diagnostics: dict = {"start_index": start + 1, "end_index": end}
            review_timed_items = [
                SubtitleItem(start + offset + 1, item.start, item.end, item.text)
                for offset, item in enumerate(items[start:end])
            ]
            batch_result = translate_texts_with_optional_review(
                texts[start:end], target_language, source_language, model, enable_review,
                model_b, review_model, source_kind, batch_diagnostics, glossary_prompt, review_timed_items
            )
            record["batches"].append(batch_diagnostics)
            for offset, text in enumerate(batch_diagnostics.get("initial_translation", batch_result)):
                record["items"][start + offset]["initial_translation"] = text
            for offset, text in enumerate(batch_result):
                record["items"][start + offset]["final_translation"] = text
            translated.extend(batch_result)
            record["status"] = "translated"
            write_translation_record(translation_record_path, record)
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        write_translation_record(translation_record_path, record)
        raise
    raw_output_items = [SubtitleItem(item.index, item.start, item.end, text) for item, text in zip(items, translated)]
    output_items, cleanup = clean_and_merge_output_subtitles(raw_output_items)
    write_srt(output_items, output_srt)
    record["output_cleanup"] = cleanup
    record["status"] = "completed"
    write_translation_record(translation_record_path, record)
    print(
        f"字幕终稿清理: empty={cleanup['removed_empty']}, "
        f"merged={cleanup['merged_adjacent_duplicates']}, output={cleanup['output_items']}"
    )
    if translation_record_path is not None:
        print(f"翻译诊断记录已保存: {translation_record_path}")


def ffmpeg_subtitle_path(path: Path) -> str:
    value = path.resolve().as_posix()
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    return value


def extract_frames_for_ocr(video: Path, out_dir: Path, ffmpeg: str, fps: float, max_frames: int, crop_bottom_percent: float) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = out_dir / "frame_%05d.jpg"
    # 只截取下方区域，减少水印/人物/背景误检。
    crop_filter = f"fps={fps},crop=iw:ih*{crop_bottom_percent / 100:.6f}:0:ih*(1-{crop_bottom_percent / 100:.6f})"
    command = [ffmpeg, "-hide_banner", "-y", "-i", str(video), "-vf", crop_filter]
    if max_frames > 0:
        command += ["-frames:v", str(max_frames)]
    command.append(str(output_pattern))
    run(command)
    return sorted(out_dir.glob("frame_*.jpg"))


def normalize_ocr_text(text: str) -> str:
    text = strip_isolated_ocr_digits(text)
    return re.sub(r"\s+", "", text).strip().casefold()


def ocr_texts_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= 0.92


def paddle_ocr_predict(ocr, frame: Path):
    if hasattr(ocr, "predict"):
        return ocr.predict(str(frame))
    try:
        return ocr.ocr(str(frame), cls=False)
    except TypeError:
        return ocr.ocr(str(frame))


def iter_ocr_lines(result):
    if not result:
        return
    pages = result if isinstance(result, list) else [result]
    for page in pages:
        if not page:
            continue
        if isinstance(page, dict):
            texts = page.get("rec_texts") or page.get("texts") or []
            scores = page.get("rec_scores") or page.get("scores") or [1.0] * len(texts)
            polys = page.get("rec_polys") or page.get("dt_polys") or page.get("boxes") or []
            for idx, text in enumerate(texts):
                points = polys[idx] if idx < len(polys) else []
                confidence = float(scores[idx]) if idx < len(scores) else 1.0
                yield points, str(text), confidence
            continue
        if isinstance(page, list):
            for line in page:
                if not line or len(line) < 2:
                    continue
                points = line[0]
                text_info = line[1]
                if isinstance(text_info, (list, tuple)) and len(text_info) >= 2:
                    text = str(text_info[0])
                    confidence = float(text_info[1])
                else:
                    text = str(text_info)
                    confidence = 1.0
                yield points, text, confidence


def points_bounds(points) -> tuple[float, float, float, float] | None:
    try:
        coords = [(float(p[0]), float(p[1])) for p in points]
    except (TypeError, ValueError, IndexError):
        return None
    if not coords:
        return None
    xs = [item[0] for item in coords]
    ys = [item[1] for item in coords]
    return min(xs), min(ys), max(xs), max(ys)


def ocr_frame_text(ocr, frame: Path, min_confidence: float) -> str:
    lines: list[tuple[float, float, str]] = []
    if hasattr(ocr, "readtext"):
        ocr_lines = ((points, text, confidence) for points, text, confidence in ocr.readtext(str(frame), detail=1, paragraph=False))
    else:
        result = paddle_ocr_predict(ocr, frame)
        ocr_lines = iter_ocr_lines(result)
    for points, text, confidence in ocr_lines:
        if confidence < min_confidence:
            continue
        text = text.strip()
        if not text:
            continue
        bounds = points_bounds(points)
        if bounds:
            min_x, min_y, _max_x, _max_y = bounds
            lines.append((min_y, min_x, text))
        else:
            lines.append((0.0, float(len(lines)), text))
    lines.sort()
    return "\n".join(item[2] for item in lines).strip()


def ocr_lang_value(language: str) -> str:
    normalized = (language or "auto").strip().lower()
    mapping = {
        "auto": "ch",
        "自动": "ch",
        "chinese": "ch",
        "中文": "ch",
        "zh": "ch",
        "ch": "ch",
        "english": "en",
        "英语": "en",
        "en": "en",
        "arabic": "arabic",
        "阿拉伯语": "arabic",
        "ar": "arabic",
    }
    return mapping.get(normalized, normalized)


def resolve_paddle_ocr_device(requested: str) -> str:
    requested = (requested or "auto").strip().lower()
    if requested == "cpu":
        return "cpu"
    preload_nvidia_runtime_dlls()
    try:
        import paddle

        cuda_available = paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0
    except Exception:
        cuda_available = False
    if requested == "cuda" and not cuda_available:
        raise RuntimeError("PaddleOCR 已指定 CUDA，但当前 PaddlePaddle 不是 GPU 版或未检测到 NVIDIA GPU。")
    return "gpu:0" if cuda_available else "cpu"


def create_ocr(language: str = "auto", device: str = "auto"):
    lang = ocr_lang_value(language)
    stage_log(f"OCR 语言模型: {lang}")
    if lang == "arabic":
        try:
            import easyocr
            import torch
        except ImportError as exc:
            raise RuntimeError("阿拉伯语 OCR 需要 EasyOCR。请执行：.\\.venv-ocr\\Scripts\\python.exe -m pip install easyocr") from exc
        requested_device = (device or "auto").strip().lower()
        use_gpu = requested_device != "cpu" and torch.cuda.is_available()
        if requested_device == "cuda" and not use_gpu:
            raise RuntimeError("EasyOCR 已指定 CUDA，但 PyTorch 未检测到可用 NVIDIA GPU。")
        stage_log(f"EasyOCR 设备: {'cuda' if use_gpu else 'cpu'}")
        return easyocr.Reader(["ar"], gpu=use_gpu, verbose=False)

    resolved_device = resolve_paddle_ocr_device(device)
    stage_log(f"PaddleOCR 设备: {resolved_device}")
    isolate_paddlex_from_torch_cuda()
    from paddleocr import PaddleOCR

    try:
        kwargs = dict(
            lang=lang,
            device=resolved_device,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        if resolved_device == "cpu":
            kwargs["enable_mkldnn"] = False
        return PaddleOCR(**kwargs)
    except (TypeError, ValueError):
        try:
            return PaddleOCR(
                use_angle_cls=False,
                lang=lang,
                show_log=False,
                use_gpu=resolved_device != "cpu",
                enable_mkldnn=False,
            )
        except (TypeError, ValueError):
            return PaddleOCR(use_angle_cls=False, lang=lang)


def ocr_hard_subtitles(
    video: Path,
    output_srt: Path,
    ffmpeg: str,
    fps: float = 2.0,
    crop_bottom_percent: float = 35.0,
    min_confidence: float = 0.55,
    min_duration: float = 0.35,
    ocr_language: str = "auto",
    ocr_device: str = "auto",
) -> None:
    if ocr_lang_value(ocr_language) != "arabic" and importlib.util.find_spec("paddleocr") is None:
        raise RuntimeError("未安装 PaddleOCR。可执行：pip install paddleocr paddlepaddle pillow")
    if fps <= 0:
        raise ValueError("OCR fps 必须大于 0")
    output_srt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hard-subtitle-ocr-") as tmp:
        started = time.perf_counter()
        stage_log(f"OCR [1/3] 抽取字幕区域画面: {video.name}")
        frames = extract_frames_for_ocr(video, Path(tmp), ffmpeg, fps=fps, max_frames=0, crop_bottom_percent=crop_bottom_percent)
        if not frames:
            raise RuntimeError("未能截取用于 OCR 的画面。")
        stage_log(f"OCR [2/3] 已抽帧 {len(frames)} 张，fps={fps}，开始加载模型")
        ocr = create_ocr(ocr_language, ocr_device)
        stage_log("OCR [3/3] 开始逐帧识别")
        items: list[SubtitleItem] = []
        active_text = ""
        active_norm = ""
        active_start: float | None = None
        last_seen: float | None = None
        frame_step = 1.0 / fps
        progress_interval = max(1, len(frames) // 20)
        for frame_index, frame in enumerate(frames):
            timestamp = frame_index * frame_step
            text = strip_isolated_ocr_digits(ocr_frame_text(ocr, frame, min_confidence))
            norm = normalize_ocr_text(text)
            if norm:
                if active_norm and ocr_texts_match(norm, active_norm):
                    last_seen = timestamp + frame_step
                    if len(norm) > len(active_norm):
                        active_text = text
                        active_norm = norm
                else:
                    if active_norm and active_start is not None and last_seen is not None and last_seen - active_start >= min_duration:
                        items.append(SubtitleItem(len(items) + 1, seconds_to_srt_time(active_start), seconds_to_srt_time(last_seen), active_text))
                    active_text = text
                    active_norm = norm
                    active_start = timestamp
                    last_seen = timestamp + frame_step
            elif active_norm and active_start is not None and last_seen is not None:
                if last_seen - active_start >= min_duration:
                    items.append(SubtitleItem(len(items) + 1, seconds_to_srt_time(active_start), seconds_to_srt_time(last_seen), active_text))
                active_text = ""
                active_norm = ""
                active_start = None
                last_seen = None
            completed = frame_index + 1
            if completed % progress_interval == 0 or completed == len(frames):
                elapsed = max(0.001, time.perf_counter() - started)
                rate = completed / elapsed
                remaining = (len(frames) - completed) / rate if rate > 0 else 0.0
                stage_log(
                    f"OCR 进度: {completed}/{len(frames)} ({completed / len(frames) * 100:.0f}%)，"
                    f"字幕段 {len(items)}，已用时 {elapsed:.1f}s，预计剩余 {remaining:.1f}s"
                )
        if active_norm and active_start is not None and last_seen is not None and last_seen - active_start >= min_duration:
            items.append(SubtitleItem(len(items) + 1, seconds_to_srt_time(active_start), seconds_to_srt_time(last_seen), active_text))
        if not items:
            raise RuntimeError("没有从硬字幕区域 OCR 出稳定字幕文本。可以改用语音识别或调高截取区域。")
        write_srt(items, output_srt)
        stage_log(f"OCR 完成: {len(items)} 条，用时 {time.perf_counter() - started:.1f}s -> {output_srt}")


def detect_hard_subtitle_region(
    video: Path,
    ffmpeg: str,
    fps: float,
    max_frames: int,
    crop_bottom_percent: float,
    margin_percent: float,
    ocr_language: str = "auto",
    ocr_device: str = "auto",
) -> dict:
    if ocr_lang_value(ocr_language) != "arabic" and importlib.util.find_spec("paddleocr") is None:
        raise RuntimeError("未安装 PaddleOCR。可执行：pip install paddleocr paddlepaddle")
    with tempfile.TemporaryDirectory(prefix="subtitle-ocr-") as tmp:
        frames = extract_frames_for_ocr(video, Path(tmp), ffmpeg, fps, max_frames, crop_bottom_percent)
        if not frames:
            raise RuntimeError("未能截取用于 OCR 的画面。")
        ocr = create_ocr(ocr_language, ocr_device)
        frame_regions: list[tuple[float, float, float, float]] = []
        accepted_boxes = 0
        for frame in frames:
            if hasattr(ocr, "readtext"):
                ocr_lines = ((points, text, confidence) for points, text, confidence in ocr.readtext(str(frame), detail=1, paragraph=False))
            else:
                result = paddle_ocr_predict(ocr, frame)
                ocr_lines = iter_ocr_lines(result)
            try:
                from PIL import Image

                with Image.open(frame) as image:
                    frame_width, frame_height = image.size
            except (OSError, ImportError):
                frame_width, frame_height = 0, 0
            current_candidates: list[tuple[float, float, float, float, float, int]] = []
            for points, _text, confidence in ocr_lines:
                if confidence < 0.55:
                    continue
                bounds = points_bounds(points)
                if not bounds or frame_width <= 0 or frame_height <= 0:
                    continue
                min_x, min_y, max_x, max_y = bounds
                center_x_ratio = (min_x + max_x) / 2 / frame_width
                left_percent = min_x / frame_width * 100
                right_percent = max_x / frame_width * 100
                top_percent = 100 - crop_bottom_percent + min_y / frame_height * crop_bottom_percent
                bottom_percent = 100 - crop_bottom_percent + max_y / frame_height * crop_bottom_percent
                center_y_percent = (top_percent + bottom_percent) / 2
                box_height_percent = bottom_percent - top_percent
                if not 0.10 <= center_x_ratio <= 0.90:
                    continue
                if not 68.0 <= center_y_percent <= 94.0:
                    continue
                if not 0.3 <= box_height_percent <= 15.0:
                    continue
                current_candidates.append((left_percent, right_percent, top_percent, bottom_percent, center_y_percent, len(normalize_ocr_text(_text))))
            if current_candidates:
                seed_box = max(current_candidates, key=lambda box: (box[5], -abs(box[4] - 79.5)))
                current_boxes = [box for box in current_candidates if abs(box[4] - seed_box[4]) <= 8.0]
                accepted_boxes += len(current_boxes)
                frame_regions.append((
                    min(box[0] for box in current_boxes),
                    max(box[1] for box in current_boxes),
                    min(box[2] for box in current_boxes),
                    max(box[3] for box in current_boxes),
                ))
        if not frame_regions:
            raise RuntimeError("没有在底部区域识别到稳定字幕。可以改用手动遮盖参数。")
        left_percent = statistics.median(region[0] for region in frame_regions)
        right_percent = statistics.median(region[1] for region in frame_regions)
        top_percent = statistics.median(region[2] for region in frame_regions)
        bottom_percent = statistics.median(region[3] for region in frame_regions)
        left_percent = max(0.0, left_percent - margin_percent)
        right_percent = min(100.0, right_percent + margin_percent)
        top_percent = max(0.0, top_percent - margin_percent)
        bottom_percent = min(100.0, bottom_percent + margin_percent)
        width_percent = max(10.0, right_percent - left_percent)
        height_percent = max(4.0, bottom_percent - top_percent)
        return {
            "cover_x_percent": round(left_percent, 2),
            "cover_width_percent": round(width_percent, 2),
            "cover_y_percent": round(top_percent, 2),
            "cover_height_percent": round(height_percent, 2),
            "box_count": accepted_boxes,
            "frames_checked": len(frames),
        }


def subtitle_style(layout: str, font_size: int, position: str, video_width: int, cover_x_percent: float, cover_width_percent: float) -> str:
    if position == "auto":
        position = "top" if layout == "bilingual" else "bottom"
    if position == "top":
        alignment, margin_v = 8, 45
    elif position == "above-original":
        alignment, margin_v = 2, 150
    else:
        alignment, margin_v = 2, 40
    margin_l = 20
    margin_r = 20
    if layout == "replace" and video_width > 0:
        safe_x = max(0.0, min(99.0, cover_x_percent))
        safe_w = max(1.0, min(100.0 - safe_x, cover_width_percent))
        margin_l = max(20, int(video_width * safe_x / 100))
        margin_r = max(20, int(video_width * max(0.0, 100.0 - safe_x - safe_w) / 100))
    return (
        f"FontName=Arial,FontSize={font_size},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
        f"Outline=2,Shadow=1,BorderStyle=1,Alignment={alignment},"
        f"MarginL={margin_l},MarginR={margin_r},MarginV={margin_v}"
    )


def render_subtitle(
    video: Path,
    subtitle: Path,
    output: Path,
    mode: str,
    layout: str,
    position: str,
    cover: bool,
    cover_x_percent: float,
    cover_y_percent: float,
    cover_width_percent: float,
    cover_height_percent: float,
    cover_opacity: float,
    cover_color: str,
    cover_auto_detect: bool,
    cover_ocr_language: str,
    font_name: str,
    font_size: int,
    quality: int,
    hardware_acceleration: str,
    ffmpeg: str,
    dry_run: bool,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if mode == "soft":
        if cover:
            print("提示: 当前为 soft/封装软字幕模式，不会烧录白色蒙版；如需蒙版请使用 burn/烧录到画面。")
        codec = "mov_text" if output.suffix.lower() == ".mp4" else "srt"
        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(video),
            "-i",
            str(subtitle),
            "-map",
            "0:v",
            "-map",
            "0:a?",
            "-map",
            "1:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            codec,
            str(output),
        ]
        run(command, dry_run)
        return
    if mode != "burn":
        raise ValueError("mode 必须是 soft 或 burn")
    if not 0 <= quality <= 51:
        raise ValueError("quality 必须在 0 到 51 之间")
    filters: list[str] = []
    if layout not in {"replace", "bilingual"}:
        raise ValueError("layout 必须是 replace 或 bilingual")
    should_cover = cover and layout == "replace"
    try:
        ffprobe = video_dedup.find_binary("ffprobe", None)
        video_info = video_dedup.probe_video(video, ffprobe)
        video_width = int(video_info.get("width") or 1080)
        video_height = int(video_info.get("height") or 1920)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
        video_width = 1080
        video_height = 1920
    if not 0 <= cover_x_percent <= 100:
        raise ValueError("cover_x_percent 必须在 0 到 100 之间")
    if not 0 < cover_width_percent <= 100 or cover_x_percent + cover_width_percent > 100:
        raise ValueError("字幕区域宽度必须大于 0，且字幕区域不能超出画面")
    if not 0 <= cover_y_percent <= 100:
        raise ValueError("cover_y_percent 必须在 0 到 100 之间")
    if not 0 < cover_height_percent <= 100 or cover_y_percent + cover_height_percent > 100:
        raise ValueError("字幕区域高度必须大于 0，且字幕区域不能超出画面")
    cover_drawbox_options = ""
    if should_cover:
        if not 0 <= cover_opacity <= 1:
            raise ValueError("cover_opacity 必须在 0 到 1 之间")
        if cover_auto_detect and not dry_run:
            try:
                region = detect_hard_subtitle_region(video, ffmpeg, fps=0.5, max_frames=30, crop_bottom_percent=35.0, margin_percent=1.5, ocr_language=cover_ocr_language)
                cover_x_percent = float(region.get("cover_x_percent", cover_x_percent))
                cover_width_percent = float(region.get("cover_width_percent", cover_width_percent))
                cover_y_percent = float(region["cover_y_percent"])
                cover_height_percent = float(region["cover_height_percent"])
                print(f"OCR 自动字幕区域: x {cover_x_percent:.2f}%，宽度 {cover_width_percent:.2f}%，y {cover_y_percent:.2f}%，高度 {cover_height_percent:.2f}%")
            except RuntimeError as exc:
                print(f"OCR 自动识别字幕区域失败，改用手动遮盖参数: {exc}", file=sys.stderr)
        x = f"iw*{cover_x_percent / 100:.6f}"
        y = f"ih*{cover_y_percent / 100:.6f}"
        w = f"iw*{cover_width_percent / 100:.6f}"
        h = f"ih*{cover_height_percent / 100:.6f}"
        cover_drawbox_options = f"drawbox=x={x}:y={y}:w={w}:h={h}:color={cover_color}@{cover_opacity:.4f}:t=fill"
    with tempfile.TemporaryDirectory(prefix="subtitle-render-") as render_tmp:
        render_items = prepare_items_for_ass_render(
            subtitle,
            video_width,
            cover_width_percent,
            font_size,
        )
        print(f"ASS 字幕事件数: {len([item for item in render_items if item.text.strip()])}")
        render_subtitle_path = write_ass_for_render(
            render_items,
            Path(render_tmp) / "render.ass",
            video_width,
            video_height,
            layout,
            font_name,
            font_size,
            position,
            cover_x_percent,
            cover_y_percent,
            cover_width_percent,
            cover_height_percent,
        )
        if should_cover and cover_drawbox_options:
            enable_expression = ffmpeg_subtitle_enable_expression(render_items)
            if enable_expression:
                filters.append(f"{cover_drawbox_options}:enable='{enable_expression}'")
            else:
                print("字幕时间轴为空，跳过旧字幕蒙版。")
        filters.append(f"subtitles=filename='{ffmpeg_subtitle_path(render_subtitle_path)}'")
        resolved_acceleration = video_dedup.resolve_hardware_acceleration(ffmpeg, hardware_acceleration)
        encoder_args: list[str]
        if resolved_acceleration == "nvidia":
            encoder_args = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(quality), "-b:v", "0"]
        elif resolved_acceleration == "amd":
            encoder_args = ["-c:v", "h264_amf", "-quality", "balanced", "-qp_i", str(quality), "-qp_p", str(quality)]
        elif resolved_acceleration == "intel":
            encoder_args = ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(quality)]
        elif resolved_acceleration == "apple":
            encoder_args = ["-c:v", "h264_videotoolbox", "-b:v", "8M"]
        else:
            encoder_args = ["-c:v", "libx264", "-preset", "medium", "-crf", str(quality)]
        print(f"实际视频编码器: {video_dedup.video_encoder_name(resolved_acceleration)} ({video_dedup.video_encoder_label(resolved_acceleration)})")
        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(video),
            "-vf",
            ",".join(filters),
            "-map",
            "0:v",
            "-map",
            "0:a?",
            *encoder_args,
            "-c:a",
            "copy",
            str(output),
        ]
        try:
            run(command, dry_run)
        except subprocess.CalledProcessError:
            if resolved_acceleration == "cpu" or dry_run:
                raise
            print("字幕烧录 GPU 编码失败，自动回退到 CPU 重试。", file=sys.stderr)
            fallback = command[:]
            encoder_start = fallback.index("-c:v")
            encoder_end = fallback.index("-c:a", encoder_start)
            fallback[encoder_start:encoder_end] = ["-c:v", "libx264", "-preset", "medium", "-crf", str(quality)]
            run(fallback, dry_run=False)
        return


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地字幕提取、翻译、替换和烧录工具")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--ffprobe", help="ffprobe 可执行文件路径")
    parser.add_argument("--log-prefix", default="", help="批量任务日志前缀，例如 [视频 2/10]")
    parser.add_argument("--dry-run", action="store_true", help="仅打印命令，不执行")
    sub = parser.add_subparsers(dest="command", required=True)

    detect = sub.add_parser("detect", help="检测视频中的软字幕轨道")
    detect.add_argument("video")

    extract = sub.add_parser("extract", help="提取软字幕轨道为 SRT/ASS")
    extract.add_argument("video")
    extract.add_argument("output")
    extract.add_argument("--stream", type=int, default=0, help="字幕流序号，默认第 0 条字幕")

    translate = sub.add_parser("translate", help="翻译或复制 SRT 字幕")
    translate.add_argument("input_srt")
    translate.add_argument("output_srt")
    translate.add_argument("--target-language", default="English")
    translate.add_argument("--source-language", default="auto")
    translate.add_argument("--provider", choices=("none", "openai-compatible"), default="none")
    translate.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "deepseek-v4-flash"))
    translate.add_argument("--source-kind", choices=("ocr", "asr", "soft"), default="ocr", help="字幕来源类型；ASR/软字幕使用更轻的清洗策略")
    translate.add_argument("--enable-llm-review", action="store_true", help="启用第二次字幕审核")
    translate.add_argument("--llm-model-b", default=os.environ.get("OPENAI_MODEL_B", ""), help="兼容旧命令，当前不再调用第二翻译模型")
    translate.add_argument("--llm-review-model", default=os.environ.get("OPENAI_REVIEW_MODEL", ""), help="审核模型，留空则复用 --model")
    translate.add_argument("--glossary-file", help="可选 JSON 术语表；同时用于初译和审核")
    translate.add_argument("--parallel-batches", type=int, default=1, help="兼容旧命令；当前每个视频固定只发送一个翻译请求")

    transcribe = sub.add_parser("transcribe", help="无字幕视频：用 faster-whisper 语音识别生成 SRT")
    transcribe.add_argument("video")
    transcribe.add_argument("output_srt")
    transcribe.add_argument("--model-size", default="medium", help="tiny/base/small/medium/large-v3 等")
    transcribe.add_argument("--language", default="auto", help="auto/zh/en/ja/ko 等")
    transcribe.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    transcribe.add_argument("--word-timestamps-output", help="可选：保存 Whisper 单词级时间戳 JSON")

    hard_ocr = sub.add_parser("hard-ocr", help="硬字幕视频：OCR 画面字幕生成 SRT")
    hard_ocr.add_argument("video")
    hard_ocr.add_argument("output_srt")
    hard_ocr.add_argument("--fps", type=float, default=2.0)
    hard_ocr.add_argument("--crop-bottom-percent", type=float, default=35.0)
    hard_ocr.add_argument("--min-confidence", type=float, default=0.55)
    hard_ocr.add_argument("--ocr-language", default="auto", help="auto/ch/en/arabic/ar/zh")
    hard_ocr.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"), help="OCR 推理设备；auto 优先 CUDA")

    region = sub.add_parser("detect-region", help="硬字幕视频：用 PaddleOCR 自动估计遮盖区域")
    region.add_argument("video")
    region.add_argument("--fps", type=float, default=0.5)
    region.add_argument("--max-frames", type=int, default=30)
    region.add_argument("--crop-bottom-percent", type=float, default=35.0)
    region.add_argument("--margin-percent", type=float, default=1.5)
    region.add_argument("--ocr-language", default="auto", help="auto/ch/en/arabic/ar/zh")
    region.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"), help="OCR 推理设备；auto 优先 CUDA")

    render = sub.add_parser("render", help="替换软字幕或烧录硬字幕")
    render.add_argument("video")
    render.add_argument("subtitle")
    render.add_argument("output")
    render.add_argument("--mode", choices=("soft", "burn"), default="burn")
    render.add_argument("--layout", choices=("replace", "bilingual"), default="replace", help="replace=覆盖原字幕；bilingual=不遮盖，在合适位置新增字幕")
    render.add_argument("--position", choices=("auto", "bottom", "above-original", "top"), default="auto")
    render.add_argument("--cover", action="store_true", help="烧录新字幕前遮住原字幕区域")
    render.add_argument("--cover-auto-detect", action="store_true", help="用 OCR 自动识别原字幕区域；失败时回退到手动百分比")
    render.add_argument("--cover-x-percent", type=float, default=0.0, help="遮盖区域从画面宽度百分比开始")
    render.add_argument("--cover-y-percent", type=float, default=74.0, help="遮盖区域从画面高度百分比开始")
    render.add_argument("--cover-width-percent", type=float, default=100.0, help="遮盖区域宽度百分比")
    render.add_argument("--cover-height-percent", type=float, default=11.0, help="遮盖区域高度百分比")
    render.add_argument("--cover-opacity", type=float, default=0.82, help="遮盖蒙版透明度")
    render.add_argument("--cover-color", default="white", help="遮盖蒙版颜色，默认 white")
    render.add_argument("--cover-ocr-language", default="auto", help="自动识别遮盖区域时使用的 OCR 语言")
    render.add_argument("--font-name", default="Arial")
    render.add_argument("--font-size", type=int, default=28)
    render.add_argument("--quality", type=int, default=15, help="烧录字幕的视频质量值，越低越清晰")
    render.add_argument("--hardware-acceleration", choices=("auto", "nvidia", "amd", "intel", "apple", "cpu"), default="auto")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    global LOG_PREFIX
    video_dedup.install_hidden_subprocess_policy()
    args = make_parser().parse_args(argv)
    LOG_PREFIX = args.log_prefix.strip()
    try:
        ffmpeg = video_dedup.find_binary("ffmpeg", args.ffmpeg)
        ffprobe = video_dedup.find_binary("ffprobe", args.ffprobe)
        if args.command == "detect":
            streams = subtitle_streams(Path(args.video), ffprobe)
            if not streams:
                print("未检测到软字幕轨道。若画面里看得到字幕，那多半是硬字幕，需要 OCR 或手动遮盖。")
            for i, stream in enumerate(streams):
                tags = stream.get("tags", {})
                print(f"[{i}] codec={stream.get('codec_name')} language={tags.get('language', '')} title={tags.get('title', '')}")
        elif args.command == "extract":
            extract_subtitle(Path(args.video), Path(args.output), args.stream, ffmpeg, args.dry_run)
        elif args.command == "translate":
            translate_srt(
                Path(args.input_srt),
                Path(args.output_srt),
                args.target_language,
                args.source_language,
                args.provider,
                args.model,
                args.parallel_batches,
                args.enable_llm_review,
                args.llm_model_b,
                args.llm_review_model,
                args.source_kind,
                glossary=(load_glossary_file(Path(args.glossary_file)) if args.glossary_file else None),
            )
        elif args.command == "transcribe":
            transcribe_video(
                Path(args.video),
                Path(args.output_srt),
                args.model_size,
                args.language,
                args.device,
                ffmpeg,
                Path(args.word_timestamps_output) if args.word_timestamps_output else None,
            )
        elif args.command == "hard-ocr":
            ocr_hard_subtitles(
                Path(args.video), Path(args.output_srt), ffmpeg, args.fps, args.crop_bottom_percent,
                args.min_confidence, ocr_language=args.ocr_language, ocr_device=args.device,
            )
        elif args.command == "detect-region":
            region = detect_hard_subtitle_region(
                Path(args.video), ffmpeg, args.fps, args.max_frames, args.crop_bottom_percent,
                args.margin_percent, args.ocr_language, args.device,
            )
            print(json.dumps(region, ensure_ascii=False, indent=2))
            print(f"建议遮盖参数：x {region.get('cover_x_percent', 0)}%，宽度 {region.get('cover_width_percent', 100)}%，起点 {region['cover_y_percent']}%，高度 {region['cover_height_percent']}%")
        elif args.command == "render":
            render_subtitle(
                Path(args.video),
                Path(args.subtitle),
                Path(args.output),
                args.mode,
                args.layout,
                args.position,
                args.cover,
                args.cover_x_percent,
                args.cover_y_percent,
                args.cover_width_percent,
                args.cover_height_percent,
                args.cover_opacity,
                args.cover_color,
                args.cover_auto_detect,
                args.cover_ocr_language,
                args.font_name,
                args.font_size,
                args.quality,
                args.hardware_acceleration,
                ffmpeg,
                args.dry_run,
            )
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
