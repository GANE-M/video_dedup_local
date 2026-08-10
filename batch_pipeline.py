#!/usr/bin/env python3
"""End-to-end folder/file pipeline.

Order is intentional:
1. read/extract/transcribe subtitles from the original video;
2. translate subtitles;
3. run the video de-dup transform;
4. write the translated subtitles into the transformed video.

That keeps subtitle detection/ASR away from later crop, speed, trim, and color
changes while still producing one final output file.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Sequence

import subtitle_tool
import video_dedup
import agent_bridge
from global_slots import global_asr_slot


VIDEO_CONFIG_KEYS = set(video_dedup.asdict(next(iter(video_dedup.PRESETS.values()))))
FINAL_SUBTITLE_DIR_NAME = "字幕终稿"
FINAL_SUBTITLE_MANIFEST_NAME = "manifest.json"
FINAL_SUBTITLE_CACHE_VERSION = 2
SERIES_ENTITY_TABLE_VERSION = 1

LOCALIZATION_INSTRUCTIONS = {
    "cinematic_standard": (
        "Use concise, natural, publication-ready screen dialogue understood across the target-language market. "
        "Prefer idiomatic subtitle phrasing over source-language word order while preserving meaning and tone."
    ),
    "conversational": (
        "Use natural contemporary spoken dialogue suitable for a short drama. Keep it concise and emotionally believable, "
        "but avoid obscure regional slang unless the source clearly requires it."
    ),
    "formal_faithful": (
        "Use faithful, polished and relatively formal target-language phrasing. Preserve ranks, relationships and plot facts "
        "with minimal colloquial adaptation."
    ),
    "gulf_neutral": (
        "For Arabic output, use accessible neutral Gulf-flavored spoken phrasing suitable for UAE and Saudi audiences, "
        "without heavy local slang; for any non-Arabic target, fall back to natural cinematic standard language."
    ),
}


def final_subtitle_language_code(language: str) -> str:
    normalized = str(language or "").strip().casefold()
    aliases = {
        "arabic": "ar", "阿拉伯语": "ar",
        "english": "en", "英语": "en",
        "chinese": "zh", "中文": "zh", "mandarin": "zh",
        "spanish": "es", "french": "fr", "german": "de",
        "portuguese": "pt", "japanese": "ja", "korean": "ko",
        "russian": "ru", "turkish": "tr", "indonesian": "id",
        "vietnamese": "vi", "thai": "th",
    }
    if normalized in aliases:
        return aliases[normalized]
    cleaned = "".join(char for char in normalized if char.isascii() and (char.isalnum() or char == "-"))
    return cleaned[:16] or "target"


def final_subtitle_cache_path(input_video: Path, target_language: str) -> Path:
    language_code = final_subtitle_language_code(target_language)
    return input_video.parent / FINAL_SUBTITLE_DIR_NAME / f"{input_video.name}.final.{language_code}.srt"


def repaired_source_subtitle_cache_path(input_video: Path, source_language: str) -> Path:
    language_code = final_subtitle_language_code(source_language)
    return input_video.parent / FINAL_SUBTITLE_DIR_NAME / f"{input_video.name}.source.{language_code}.srt"


def repaired_source_language(args: argparse.Namespace, item: dict | None = None) -> str:
    requested = str(getattr(args, "source_language", "") or "").strip()
    if requested and requested.casefold() not in {"auto", "automatic", "自动"}:
        return requested
    if item:
        for field in ("audio_path", "visual_path", "repaired_source_srt"):
            candidate = item.get(field)
            if not candidate:
                continue
            try:
                text = " ".join(row.text for row in subtitle_tool.parse_srt(Path(candidate)))
            except (OSError, ValueError, TypeError):
                continue
            arabic = sum("\u0600" <= char <= "\u06ff" for char in text)
            chinese = sum("\u3400" <= char <= "\u9fff" for char in text)
            latin = sum(char.isascii() and char.isalpha() for char in text)
            if max(arabic, chinese, latin) <= 0:
                continue
            if arabic >= chinese and arabic >= latin:
                return "Arabic"
            if chinese >= arabic and chinese >= latin:
                return "Chinese"
            return "English"
    if item:
        visual_kind = str(item.get("visual_kind") or "")
        if visual_kind in {"ocr", "soft"}:
            ocr_language = str(getattr(args, "ocr_language", "") or "").strip()
            if ocr_language and ocr_language.casefold() not in {"auto", "automatic", "自动"}:
                return _ocr_language_name(ocr_language, requested)
    return "Unknown"


def series_entity_table_path(input_path: Path, target_language: str) -> Path:
    source_dir = input_path if input_path.is_dir() else input_path.parent
    language_code = final_subtitle_language_code(target_language)
    return source_dir / FINAL_SUBTITLE_DIR_NAME / f"series_entities.{language_code}.json"


def load_series_entity_table(input_path: Path, target_language: str) -> dict:
    path = series_entity_table_path(input_path, target_language)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"version": SERIES_ENTITY_TABLE_VERSION, "target_language": target_language, "entities": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("entities"), list):
        return {"version": SERIES_ENTITY_TABLE_VERSION, "target_language": target_language, "entities": []}
    return payload


def save_series_entity_table(input_path: Path, target_language: str, entities: list[dict], job_id: str) -> Path:
    path = series_entity_table_path(input_path, target_language)
    payload = {
        "version": SERIES_ENTITY_TABLE_VERSION,
        "target_language": target_language,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated_by_job": job_id,
        "entities": entities,
    }
    _atomic_write_json(path, payload)
    return path


def merge_series_entities(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Merge per-job discoveries without making the fast-mode table an input."""
    merged: list[dict] = []
    positions: dict[tuple[str, str], int] = {}
    for entity in [*existing, *incoming]:
        if not isinstance(entity, dict):
            continue
        source = str(entity.get("source", "")).strip()
        if not source and isinstance(entity.get("source_aliases"), list) and entity["source_aliases"]:
            source = str(entity["source_aliases"][0]).strip()
        entity_type = str(entity.get("type") or entity.get("kind") or "unknown").strip().casefold() or "unknown"
        target = str(entity.get("target") or entity.get("preferred_target") or entity.get("canonical_target") or "").strip()
        if not source or not target:
            continue
        key = (entity_type, source.casefold())
        normalized = dict(entity)
        normalized["source"] = source
        normalized["target"] = target
        if key not in positions:
            positions[key] = len(merged)
            merged.append(normalized)
            continue
        previous = merged[positions[key]]
        try:
            previous_confidence = float(previous.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            previous_confidence = 0.0
        try:
            incoming_confidence = float(normalized.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            incoming_confidence = 0.0
        if incoming_confidence >= previous_confidence:
            replacement = normalized
        else:
            replacement = dict(previous)
        for field in ("aliases", "evidence_episodes"):
            values = []
            for value in [*(previous.get(field) or []), *(normalized.get(field) or [])]:
                if value not in values:
                    values.append(value)
            if values:
                replacement[field] = values
        merged[positions[key]] = replacement
    return merged


def load_existing_final_subtitle_references(input_path: Path, target_language: str) -> list[dict]:
    """Load valid cached finals as read-only context for advanced Agent work."""
    source_dir = input_path if input_path.is_dir() else input_path.parent
    cache_dir = source_dir / FINAL_SUBTITLE_DIR_NAME
    manifest = _read_final_subtitle_manifest(cache_dir)
    language_code = final_subtitle_language_code(target_language)
    references = []
    for entry in manifest.get("entries", {}).values():
        if not isinstance(entry, dict) or entry.get("language_code") != language_code:
            continue
        subtitle_path = cache_dir / str(entry.get("subtitle_file", ""))
        source_video = source_dir / str(entry.get("source_name", ""))
        try:
            if not subtitle_path.is_file() or not source_video.is_file():
                continue
            if entry.get("source_fingerprint") != lightweight_source_fingerprint(source_video):
                continue
            rows = subtitle_tool.parse_srt(subtitle_path)
        except (OSError, ValueError, TypeError):
            continue
        references.append(
            {
                "video_name": source_video.name,
                "target_language": target_language,
                "read_only": True,
                "subtitles": [
                    {
                        "index": row.index,
                        "start": row.start,
                        "end": row.end,
                        "text": subtitle_tool.normalize_target_language_text(row.text, target_language),
                    }
                    for row in rows
                ],
            }
        )
    return references


def lightweight_source_fingerprint(path: Path) -> dict:
    """Identify a source video without hashing every byte of a large file."""
    stat = path.stat()
    chunk_size = 1024 * 1024
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode("ascii"))
    with path.open("rb") as source:
        digest.update(source.read(chunk_size))
        if stat.st_size > chunk_size:
            source.seek(max(0, stat.st_size - chunk_size))
            digest.update(source.read(chunk_size))
    return {"size": stat.st_size, "sample_sha256": digest.hexdigest()}


def batch_checkpoint_path(args: argparse.Namespace, input_video: Path, index: int) -> Path | None:
    root = str(getattr(args, "checkpoint_dir", "") or "").strip()
    if not root:
        return None
    name = f"{index:04d}-{safe_record_stem(input_video.name)}.json"
    return Path(root).resolve() / name


def batch_checkpoint_signature(
    args: argparse.Namespace,
    input_video: Path,
    output_video: Path,
) -> dict:
    config_path = Path(args.config).resolve()
    config_digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "source": lightweight_source_fingerprint(input_video),
        "source_name": input_video.name,
        "output_name": output_video.name,
        "config_sha256": config_digest,
        "preset": args.preset,
        "subtitles": bool(args.enable_subtitles),
        "subtitle_only": bool(getattr(args, "subtitle_only", False)),
        "target_language": str(getattr(args, "target_language", "")),
    }


def completed_batch_checkpoint(
    args: argparse.Namespace,
    input_video: Path,
    output_video: Path,
    index: int,
) -> bool:
    path = batch_checkpoint_path(args, input_video, index)
    if path is None or not path.is_file() or not output_video.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if payload.get("status") != "completed":
            return False
        if payload.get("signature") != batch_checkpoint_signature(args, input_video, output_video):
            return False
        return output_video.stat().st_size > 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def save_batch_checkpoint(
    args: argparse.Namespace,
    input_video: Path,
    output_video: Path,
    index: int,
) -> None:
    path = batch_checkpoint_path(args, input_video, index)
    if path is None:
        return
    payload = {
        "status": "completed",
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "signature": batch_checkpoint_signature(args, input_video, output_video),
        "output_size": output_video.stat().st_size,
    }
    _atomic_write_json(path, payload)


def _read_final_subtitle_manifest(cache_dir: Path) -> dict:
    manifest_path = cache_dir / FINAL_SUBTITLE_MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"version": FINAL_SUBTITLE_CACHE_VERSION, "entries": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        return {"version": FINAL_SUBTITLE_CACHE_VERSION, "entries": {}}
    return payload


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_final_subtitle_cache(
    args: argparse.Namespace,
    input_video: Path,
    output_video: Path,
    seed: int | None,
    index: int,
    total: int,
) -> dict | None:
    if getattr(args, "no_final_subtitle_cache", False) or not input_video.is_file():
        return None
    if getattr(args, "translation_quality", "fast") == "advanced":
        # Advanced mode deliberately rebuilds OCR/ASR evidence and sends any
        # existing final SRT back as read-only draft/context for refinement.
        # Selecting fast mode later still reuses the advanced final normally.
        print(f"[视频 {index}/{total}] 高级翻译：保留已有终稿作为参考，重新准备原文证据")
        return None
    cache_path = final_subtitle_cache_path(input_video, args.target_language)
    manifest = _read_final_subtitle_manifest(cache_path.parent)
    entry_key = f"{input_video.name}|{final_subtitle_language_code(args.target_language)}"
    entry = manifest.get("entries", {}).get(entry_key)
    if not cache_path.is_file() or not isinstance(entry, dict):
        return None
    try:
        paired_source_file = str(entry.get("paired_source_file") or "")
        if (
            int(manifest.get("version", 1) or 1) < FINAL_SUBTITLE_CACHE_VERSION
            or not paired_source_file
            or not (cache_path.parent / paired_source_file).is_file()
        ):
            print(
                f"[视频 {index}/{total}] 字幕终稿缺少修复原文资产，"
                "重新执行一次 OCR/ASR 与审核以补全中英阿对照"
            )
            return None
        if entry.get("source_fingerprint") != lightweight_source_fingerprint(input_video):
            print(f"[视频 {index}/{total}] 字幕终稿存在，但源视频指纹不匹配，重新识别和翻译")
            return None
        cached_items = subtitle_tool.parse_srt(cache_path)
        if not cached_items or int(entry.get("subtitle_count", -1)) != len(cached_items):
            print(f"[视频 {index}/{total}] 字幕终稿校验失败，重新识别和翻译")
            return None
        work_dir = Path(args.translation_run_dir) / "_working" / f"{index:03d}-{safe_record_stem(input_video.stem)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        translated_srt = work_dir / "translated.srt"
        normalized_cached_items = [
            subtitle_tool.SubtitleItem(
                item.index,
                item.start,
                item.end,
                subtitle_tool.normalize_target_language_text(item.text, args.target_language),
            )
            for item in cached_items
        ]
        subtitle_tool.write_srt(normalized_cached_items, translated_srt)
    except (OSError, ValueError, TypeError) as exc:
        print(f"[视频 {index}/{total}] 读取字幕终稿失败，重新识别和翻译: {exc}")
        return None
    print(f"[视频 {index}/{total}] 命中字幕终稿缓存: {cache_path}（跳过 OCR/ASR/Agent/API）")
    return {
        "index": index,
        "total": total,
        "input_video": input_video,
        "output_video": output_video,
        "seed": seed,
        "translated_srt": translated_srt,
        "final_subtitle_cache_hit": True,
        "final_subtitle_cache_path": cache_path,
    }


def save_final_subtitle_caches(args: argparse.Namespace, prepared: list[dict]) -> None:
    """Persist repaired source and translated original-timeline subtitle assets."""
    manifests: dict[Path, dict] = {}
    for item in prepared:
        if item.get("final_subtitle_cache_hit"):
            continue
        input_video = Path(item["input_video"])
        translated_srt = Path(item["translated_srt"])
        try:
            subtitles = subtitle_tool.parse_srt(translated_srt)
            if not subtitles:
                raise ValueError("终稿字幕为空")
            source_fingerprint = lightweight_source_fingerprint(input_video)
            cache_path = final_subtitle_cache_path(input_video, args.target_language)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
            shutil.copyfile(translated_srt, temporary)
            os.replace(temporary, cache_path)
            manifest = manifests.setdefault(cache_path.parent, _read_final_subtitle_manifest(cache_path.parent))
            manifest["version"] = FINAL_SUBTITLE_CACHE_VERSION
            manifest["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            entries = manifest.setdefault("entries", {})
            language_code = final_subtitle_language_code(args.target_language)
            entries[f"{input_video.name}|{language_code}"] = {
                "source_name": input_video.name,
                "source_fingerprint": source_fingerprint,
                "asset_kind": "translation_final",
                "target_language": args.target_language,
                "language_code": language_code,
                "subtitle_file": cache_path.name,
                "subtitle_count": len(subtitles),
                "translation_quality": getattr(args, "translation_quality", "fast"),
                "localization_strategy": getattr(args, "localization_strategy", "cinematic_standard"),
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "timeline": "original_source_video",
            }
            repaired_path_value = item.get("repaired_source_srt")
            if repaired_path_value:
                repaired_path = Path(repaired_path_value)
                repaired_items = subtitle_tool.parse_srt(repaired_path)
                if repaired_items:
                    source_language = repaired_source_language(args, item)
                    source_code = final_subtitle_language_code(source_language)
                    source_cache_path = repaired_source_subtitle_cache_path(input_video, source_language)
                    source_temporary = source_cache_path.with_name(
                        f".{source_cache_path.name}.{os.getpid()}.tmp"
                    )
                    shutil.copyfile(repaired_path, source_temporary)
                    os.replace(source_temporary, source_cache_path)
                    source_key = f"{input_video.name}|source|{source_code}"
                    entries[source_key] = {
                        "source_name": input_video.name,
                        "source_fingerprint": source_fingerprint,
                        "asset_kind": "source_repaired",
                        "source_language": source_language,
                        "language_code": source_code,
                        "subtitle_file": source_cache_path.name,
                        "subtitle_count": len(repaired_items),
                        "repair_method": item.get("source_repair_method", "aligned_evidence_cleanup"),
                        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "timeline": "original_source_video",
                    }
                    entries[f"{input_video.name}|{language_code}"]["paired_source_file"] = source_cache_path.name
                    entries[f"{input_video.name}|{language_code}"]["source_language"] = source_language
                    entries[f"{input_video.name}|{language_code}"]["source_language_code"] = source_code
                    item["repaired_source_cache_path"] = source_cache_path
                    print(
                        f"[视频 {item['index']}/{item['total']}] 已保存修复原文: "
                        f"{source_cache_path}"
                    )
            item["final_subtitle_cache_path"] = cache_path
            print(f"[视频 {item['index']}/{item['total']}] 已保存字幕终稿: {cache_path}")
        except (OSError, ValueError, TypeError) as exc:
            print(f"[视频 {item['index']}/{item['total']}] 警告：字幕终稿保存失败，不影响本次成片: {exc}", file=sys.stderr)
    for cache_dir, manifest in manifests.items():
        try:
            _atomic_write_json(cache_dir / FINAL_SUBTITLE_MANIFEST_NAME, manifest)
        except OSError as exc:
            print(f"警告：字幕终稿清单保存失败: {cache_dir / FINAL_SUBTITLE_MANIFEST_NAME}: {exc}", file=sys.stderr)


def safe_record_stem(value: str) -> str:
    cleaned = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in value)
    return cleaned.strip(" ._")[:100] or "video"


def hidden_subprocess_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


def collect_pipeline_inputs(input_path: Path, input_list: Path | None) -> list[Path]:
    if input_list:
        raw_inputs = json.loads(input_list.read_text(encoding="utf-8-sig"))
        if not isinstance(raw_inputs, list):
            raise ValueError("input-list 必须是 JSON 路径数组")
        inputs = [Path(item).resolve() for item in raw_inputs]
    else:
        inputs = video_dedup.collect_inputs(input_path.resolve())
    invalid = [item for item in inputs if not item.is_file() or item.suffix.lower() not in video_dedup.VIDEO_SUFFIXES]
    if invalid:
        raise ValueError(f"无效的视频文件: {invalid[0]}")
    return inputs


def run_video_transform(
    input_video: Path,
    output_video: Path,
    preset: str,
    config: Path,
    seed: int | None,
    hardware_acceleration: str,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    command = [
        sys.executable,
        "-u",
        str(Path(__file__).with_name("video_dedup.py")),
        str(input_video),
        str(output_video),
        "--preset",
        preset,
        "--config",
        str(config),
        "--hardware-acceleration",
        hardware_acceleration,
        "--ffmpeg",
        ffmpeg,
        "--ffprobe",
        ffprobe,
    ]
    if seed is not None:
        command += ["--seed", str(seed)]
    print("视频去重处理:")
    print(subprocess.list2cmdline(command))
    subprocess.run(command, check=True, **hidden_subprocess_kwargs())


def _run_source_subprocess(command: list[str], label: str, timeout_seconds: int) -> None:
    print(f"{label}启动，超时上限 {timeout_seconds} 秒")
    process = subprocess.Popen(command, **hidden_subprocess_kwargs())
    try:
        code = process.wait(timeout=None if timeout_seconds <= 0 else timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, **hidden_subprocess_kwargs())
        else:
            process.kill()
        process.wait()
        raise RuntimeError(f"{label}超过 {timeout_seconds} 秒，已终止并降级到另一字幕来源。") from exc
    if code != 0:
        raise subprocess.CalledProcessError(code, command)


def _asr_language_code(source_language: str) -> str:
    return {
        "chinese": "zh", "中文": "zh", "zh": "zh",
        "english": "en", "英语": "en", "en": "en",
        "arabic": "ar", "阿拉伯语": "ar", "ar": "ar",
    }.get(source_language.strip().casefold(), "auto")


def _asr_python_executable() -> str:
    """Use the isolated ASR environment so Torch/Paddle and CTranslate2 do not share cuDNN DLLs."""
    configured = str(os.environ.get("VIDEO_TOOL_ASR_PYTHON") or "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if not candidate.is_file():
            raise RuntimeError(f"VIDEO_TOOL_ASR_PYTHON 指向的解释器不存在: {candidate}")
        return str(candidate)
    project_root = Path(__file__).resolve().parent
    candidate = (
        project_root / ".venv-asr" / "Scripts" / "python.exe"
        if os.name == "nt"
        else project_root / ".venv-asr" / "bin" / "python"
    )
    if candidate.is_file():
        return str(candidate)
    if os.name == "nt":
        raise RuntimeError(
            "Windows GPU ASR 独立环境未安装。请在项目目录执行："
            "py -3.12 -m venv .venv-asr；"
            r".\.venv-asr\Scripts\python.exe -m pip install -r requirements-asr.txt"
        )
    return sys.executable


def _ocr_language_name(ocr_language: str, fallback: str) -> str:
    return {
        "ch": "Chinese", "zh": "Chinese", "chinese": "Chinese", "中文": "Chinese",
        "en": "English", "english": "English", "英语": "English",
        "arabic": "Arabic", "ar": "Arabic", "阿拉伯语": "Arabic",
    }.get(ocr_language.strip().casefold(), fallback)


def make_subtitle_sources(
    input_video: Path,
    visual_srt: Path,
    asr_srt: Path,
    source_mode: str,
    ocr_language: str,
    source_language: str,
    whisper_model: str,
    whisper_device: str,
    ffmpeg: str,
    ffprobe: str,
    ocr_timeout_seconds: int,
    asr_timeout_seconds: int,
    log_prefix: str = "",
    ocr_device: str = "auto",
    global_asr_workers: int = 5,
) -> dict[str, Path]:
    def source_log(message: str) -> None:
        print(f"{log_prefix} {message}" if log_prefix else message)

    streams = subtitle_tool.subtitle_streams(input_video, ffprobe)
    should_extract = source_mode in {"soft", "soft-asr"} or (source_mode in {"auto", "auto-ocr"} and streams)
    if should_extract:
        if not streams:
            raise ValueError("选择了软字幕来源，但视频没有软字幕轨道。")
        source_log("字幕来源: 软字幕轨道")
        try:
            subtitle_tool.extract_subtitle(input_video, visual_srt, 0, ffmpeg, dry_run=False)
            if source_mode == "soft-asr":
                source_log("软字幕轨道可用：继续并行执行音频 ASR，用于交叉审核。")
            else:
                source_log("软字幕轨道可用：视为可靠文本，跳过 OCR 与音频 ASR。")
                return {"soft": visual_srt}
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            if source_mode in {"soft", "soft-asr"}:
                raise
            visual_srt.unlink(missing_ok=True)
            print(f"软字幕轨道无法转换为文本，继续尝试画面 OCR: {exc}", file=sys.stderr)

    subtitle_cli = str(Path(__file__).with_name("subtitle_tool.py"))
    asr_words = asr_srt.with_suffix(".words.json")

    def run_ocr() -> None:
        command = [
            sys.executable, "-u", subtitle_cli, "--ffmpeg", ffmpeg, "--log-prefix", log_prefix,
            "hard-ocr", str(input_video), str(visual_srt), "--ocr-language", ocr_language,
            "--device", ocr_device,
        ]
        _run_source_subprocess(command, f"{log_prefix} 硬字幕 OCR".strip(), ocr_timeout_seconds)

    def run_asr() -> None:
        command = [
            _asr_python_executable(), "-u", subtitle_cli, "--ffmpeg", ffmpeg, "--log-prefix", log_prefix,
            "transcribe", str(input_video), str(asr_srt),
            "--model-size", whisper_model,
            "--language", _asr_language_code(source_language),
            "--device", whisper_device,
            "--word-timestamps-output", str(asr_words),
        ]
        asr_label = f"{log_prefix} 音频 ASR".strip()
        # Waiting for a shared slot is queue time, not Whisper runtime, so it
        # deliberately happens outside the per-video ASR timeout.
        with global_asr_slot(global_asr_workers, "音频 ASR", source_log):
            _run_source_subprocess(command, asr_label, asr_timeout_seconds)

    if source_mode == "hard-ocr" or source_mode == "auto-ocr":
        try:
            source_log("字幕来源: 画面硬字幕 OCR")
            run_ocr()
            return {"ocr": visual_srt}
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
            raise

    if source_mode == "asr":
        source_log("字幕来源: 音频 ASR")
        run_asr()
        return {"asr": asr_srt}

    if source_mode == "soft-asr":
        source_log("字幕来源: 软字幕 + 音频 ASR 并行双源")
    else:
        source_log("字幕来源: 硬字幕 OCR + 音频 ASR 并行双源")
    jobs: dict[str, object] = {}
    results: dict[str, Path] = {}
    errors: dict[str, Exception] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="subtitle-source") as executor:
        if source_mode == "soft-asr" and visual_srt.is_file():
            results["soft"] = visual_srt
        else:
            jobs["ocr"] = executor.submit(run_ocr)
        jobs["asr"] = executor.submit(run_asr)
        future_kinds = {future: kind for kind, future in jobs.items()}
        for future in concurrent.futures.as_completed(future_kinds):
            kind = future_kinds[future]
            try:
                future.result()
                path = visual_srt if kind == "ocr" else asr_srt
                if path.is_file() and subtitle_tool.parse_srt(path):
                    results[kind] = path
                    source_log(f"字幕分支完成: {kind.upper()}，{len(subtitle_tool.parse_srt(path))} 条")
            except Exception as exc:
                errors[kind] = exc
                print(f"{log_prefix} {kind.upper()} 来源失败，继续使用另一来源: {exc}".strip(), file=sys.stderr)
    if results:
        if "asr" in results and asr_words.is_file():
            results["asr_words"] = asr_words
        source_log(f"字幕来源阶段完成: {', '.join(results)}")
        return results
    detail = "; ".join(f"{kind}={error}" for kind, error in errors.items())
    raise RuntimeError(f"OCR 与音频 ASR 均失败: {detail}")


def resolved_ocr_language(ocr_language: str, source_language: str) -> str:
    if ocr_language == "auto" and source_language.strip().casefold() in {"arabic", "ar", "阿拉伯语"}:
        return "arabic"
    return ocr_language


def write_repaired_source_from_evidence(
    args: argparse.Namespace,
    item: dict,
    output_srt: Path,
) -> Path:
    """Materialize the best cleaned source-language evidence without translating it."""
    visual_path = item.get("visual_path")
    audio_path = item.get("audio_path")
    visual_items = subtitle_tool.parse_srt(Path(visual_path)) if visual_path else []
    audio_items = subtitle_tool.parse_srt(Path(audio_path)) if audio_path else []
    visual_kind = str(item.get("visual_kind") or "ocr")
    if visual_items and audio_items:
        pairs = subtitle_tool.align_visual_and_audio_subtitles(
            visual_items,
            audio_items,
            audio_words=subtitle_tool.load_asr_words(item.get("audio_words_path")),
        )
        pairs, _grouping = subtitle_tool.group_aligned_subtitle_pairs(pairs, visual_kind)
    elif visual_items:
        pairs = [
            subtitle_tool.AlignedSubtitlePair(
                index=position,
                start=source.start,
                end=source.end,
                visual_text=source.text,
                audio_text="",
                source_indexes=(source.index,),
                visual_fragments=(source.text,),
            )
            for position, source in enumerate(visual_items, 1)
        ]
    else:
        pairs = [
            subtitle_tool.AlignedSubtitlePair(
                index=position,
                start=source.start,
                end=source.end,
                visual_text="",
                audio_text=source.text,
                audio_confidence=1.0,
                temporal_confidence=1.0,
                source_indexes=(source.index,),
            )
            for position, source in enumerate(audio_items, 1)
        ]
    if not pairs:
        raise ValueError("没有可保存的原文字幕证据")

    source_language = repaired_source_language(args, item)
    source_code = final_subtitle_language_code(source_language)
    visual_language = _ocr_language_name(args.ocr_language, args.source_language)
    visual_code = final_subtitle_language_code(visual_language)
    audio_code = final_subtitle_language_code(args.source_language)
    repaired: list[subtitle_tool.SubtitleItem] = []
    for pair in pairs:
        subtitle_tool.score_aligned_pair(
            pair,
            visual_language,
            args.source_language,
            visual_kind,
        )
        visual = subtitle_tool.clean_text_for_translation(pair.visual_text, visual_kind).strip()
        audio = subtitle_tool.clean_text_for_translation(pair.audio_text, "asr").strip()
        if source_code == audio_code and audio:
            text = audio
        elif source_code == visual_code and visual:
            text = visual
        elif visual_kind == "soft" and visual:
            text = visual
        elif audio and (not visual or pair.audio_confidence >= subtitle_tool.source_text_quality(pair.visual_text, visual_kind)):
            text = audio
        else:
            text = visual or audio
        if text:
            repaired.append(
                subtitle_tool.SubtitleItem(pair.index, pair.start, pair.end, text)
            )
    if not repaired:
        raise ValueError("原文字幕证据清洗后为空")
    subtitle_tool.write_srt(repaired, output_srt)
    return output_srt


def adjusted_subtitle_for_transform(source_srt: Path, output_srt: Path, input_video: Path, config: video_dedup.TransformConfig, ffprobe: str) -> Path:
    info = video_dedup.probe_video(input_video, ffprobe)
    subtitle_tool.adjust_srt_timing(
        source_srt,
        output_srt,
        trim_start=config.trim_start,
        trim_end=config.trim_end,
        speed=config.speed,
        source_duration=info["duration"],
    )
    return output_srt


def sanitize_video_config(config_path: Path, output_path: Path) -> Path:
    values = json.loads(config_path.read_text(encoding="utf-8-sig"))
    values = {key: value for key, value in values.items() if key in VIDEO_CONFIG_KEYS}
    output_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    return output_path


def estimate_visible_tokens(value: object) -> int:
    """Cheap, explicitly approximate count for persisted Agent reports."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    ascii_count = sum(ord(char) < 128 for char in text)
    non_ascii_count = len(text) - ascii_count
    return max(1, round(ascii_count / 4.0 + non_ascii_count / 1.8))


def prepare_video_sources_for_agent(
    args: argparse.Namespace,
    input_video: Path,
    output_video: Path,
    seed: int | None,
    index: int,
    total: int,
) -> dict:
    """Extract local subtitle evidence without calling any translation API."""
    progress = f"[视频 {index}/{total}]"

    def log(message: str) -> None:
        print(f"{progress} {message}")

    ffmpeg = video_dedup.find_binary("ffmpeg", args.ffmpeg)
    ffprobe = video_dedup.find_binary("ffprobe", args.ffprobe)
    work_dir = Path(args.translation_run_dir) / "_working" / f"{index:03d}-{safe_record_stem(input_video.stem)}"
    work_dir.mkdir(parents=True, exist_ok=True)
    visual_srt = work_dir / "visual_source.srt"
    asr_srt = work_dir / "audio_asr_source.srt"
    repaired_source_srt = work_dir / "repaired_source.srt"
    translated_srt = work_dir / "translated.srt"
    log("[Agent 阶段 1/3] 提取 OCR/软字幕与音频 ASR 材料")
    sources = None
    for source_attempt in range(1, 4):
        try:
            sources = make_subtitle_sources(
                input_video,
                visual_srt,
                asr_srt,
                args.subtitle_source,
                resolved_ocr_language(args.ocr_language, args.source_language),
                args.source_language,
                args.whisper_model,
                args.whisper_device,
                ffmpeg,
                ffprobe,
                args.ocr_timeout_seconds,
                args.asr_timeout_seconds,
                progress,
                ocr_device=args.ocr_device,
                global_asr_workers=getattr(args, "global_asr_workers", 5),
            )
            break
        except RuntimeError as exc:
            # Only a total evidence failure is worth re-running. A surviving
            # source is intentionally accepted by make_subtitle_sources.
            if "OCR" not in str(exc) or "ASR" not in str(exc) or source_attempt >= 3:
                raise
            delay = 2 ** source_attempt
            log(f"OCR 与 ASR 均失败，{delay} 秒后重试来源阶段 ({source_attempt}/3)")
            visual_srt.unlink(missing_ok=True)
            asr_srt.unlink(missing_ok=True)
            asr_srt.with_suffix(".words.json").unlink(missing_ok=True)
            time.sleep(delay)
    if not sources:
        raise RuntimeError("字幕来源阶段未返回结果")
    visual_kind = "soft" if "soft" in sources else "ocr" if "ocr" in sources else None
    visual_path = sources.get("soft") or sources.get("ocr")
    audio_path = sources.get("asr")
    audio_words_path = sources.get("asr_words")
    log(f"[Agent 阶段 1/3] 完成，来源={'+'.join(sources)}")
    return {
        "index": index,
        "total": total,
        "input_video": input_video,
        "output_video": output_video,
        "seed": seed,
        "translated_srt": translated_srt,
        "repaired_source_srt": repaired_source_srt,
        "source_repair_method": "agent_semantic_repair",
        "visual_kind": visual_kind,
        "visual_path": visual_path,
        "audio_path": audio_path,
        "audio_words_path": audio_words_path,
    }


def run_agent_translation(args: argparse.Namespace, prepared: list[dict]) -> tuple[Path, Path]:
    """Submit one folder-level job and materialize the returned SRT files."""
    bridge_root = Path(args.agent_bridge_root).resolve()
    quality_mode = str(getattr(args, "translation_quality", "fast") or "fast")
    localization_strategy = str(getattr(args, "localization_strategy", "cinematic_standard") or "cinematic_standard")
    localization_instruction = LOCALIZATION_INSTRUCTIONS.get(
        localization_strategy, LOCALIZATION_INSTRUCTIONS["cinematic_standard"]
    )
    entity_table = (
        load_series_entity_table(Path(args.input).resolve(), args.target_language)
        if quality_mode == "advanced"
        else {"version": SERIES_ENTITY_TABLE_VERSION, "target_language": args.target_language, "entities": []}
    )
    existing_final_subtitles = (
        load_existing_final_subtitle_references(Path(args.input).resolve(), args.target_language)
        if quality_mode == "advanced"
        else []
    )
    episodes = []
    translation_contracts: dict[str, str] = {}
    for item in prepared:
        visual_path = item.get("visual_path")
        audio_path = item.get("audio_path")
        visual_items = subtitle_tool.parse_srt(Path(visual_path)) if visual_path else []
        audio_items = subtitle_tool.parse_srt(Path(audio_path)) if audio_path else []
        visual_kind = item.get("visual_kind") or "ocr"
        if visual_items and audio_items:
            pairs = subtitle_tool.align_visual_and_audio_subtitles(
                visual_items,
                audio_items,
                audio_words=subtitle_tool.load_asr_words(item.get("audio_words_path")),
            )
            pairs, grouping = subtitle_tool.group_aligned_subtitle_pairs(pairs, visual_kind)
        elif visual_items:
            pairs = [
                subtitle_tool.AlignedSubtitlePair(
                    index=position,
                    start=source.start,
                    end=source.end,
                    visual_text=source.text,
                    audio_text="",
                    source_indexes=(source.index,),
                    visual_fragments=(source.text,),
                )
                for position, source in enumerate(visual_items, 1)
            ]
            grouping = {"mode": "single_visual_source", "input": len(visual_items), "output": len(pairs)}
        else:
            pairs = [
                subtitle_tool.AlignedSubtitlePair(
                    index=position,
                    start=source.start,
                    end=source.end,
                    visual_text="",
                    audio_text=source.text,
                    audio_confidence=1.0,
                    temporal_confidence=1.0,
                    source_indexes=(source.index,),
                )
                for position, source in enumerate(audio_items, 1)
            ]
            grouping = {"mode": "single_audio_source", "input": len(audio_items), "output": len(pairs)}
        if not pairs:
            raise RuntimeError(f"Agent episode {item['index']} has no usable aligned subtitle evidence")
        for pair in pairs:
            subtitle_tool.score_aligned_pair(
                pair,
                _ocr_language_name(args.ocr_language, args.source_language),
                args.source_language,
                visual_kind,
            )
        aligned_items = subtitle_tool.aligned_pairs_payload(pairs, visual_kind)
        expected_subtitle_indexes = [int(row["index"]) for row in aligned_items]
        source_output_language = repaired_source_language(args, item)
        contract_id = f"dual-source-{visual_kind}"
        translation_contracts.setdefault(
            contract_id,
            subtitle_tool.build_dual_source_translation_prompt(
                args.target_language,
                _ocr_language_name(args.ocr_language, args.source_language),
                args.source_language,
                visual_kind,
                subtitle_tool.build_glossary_prompt(args.glossary_data),
                localization_instruction,
            ),
        )
        episodes.append(
            {
                "index": int(item["index"]),
                "video_name": Path(item["input_video"]).name,
                "video_path": str(Path(item["input_video"]).resolve()),
                "visual_source_kind": visual_kind,
                "expected_subtitle_indexes": expected_subtitle_indexes,
                "source_output": {
                    "required": True,
                    "language": source_output_language,
                    "purpose": (
                        "Semantically repaired source-language subtitles reconstructed from OCR/soft-subtitle "
                        "and ASR evidence; remove watermarks/UI/noise without translating through the target language."
                    ),
                },
                "grouping": grouping,
                "items": aligned_items,
                "translation_contract_id": contract_id,
            }
        )
    payload = {
        "task_type": "folder_subtitle_translation",
        "title": getattr(args, "agent_task_title", "") or Path(args.input).resolve().name,
        "input_directory": str(Path(args.input).resolve()),
        "output_directory": str(Path(args.output).resolve()),
        "target_language": args.target_language,
        "source_language": args.source_language,
        "ocr_language": args.ocr_language,
        "subtitle_source": args.subtitle_source,
        "translation_quality": quality_mode,
        "localization_strategy": localization_strategy,
        "localization_instruction": localization_instruction,
        "max_parallel": max(1, int(args.video_workers)),
        "glossary": args.glossary_data,
        "series_entity_table": entity_table,
        "existing_final_subtitles": existing_final_subtitles,
        "quality_policy": {
            "minimum_score": 9.5 if quality_mode == "advanced" else 8.5,
            "maximum_revision_cycles": 3 if quality_mode == "advanced" else 1,
            "required_stages": (
                ["reliable_draft", "native_localization_refinement", "independent_series_final_review"]
                if quality_mode == "advanced"
                else ["translation", "episode_self_review", "series_self_review"]
            ),
            "advanced_patch_only_refinement": quality_mode == "advanced",
            "require_independent_final_review": quality_mode == "advanced",
        },
        "translation_contracts": translation_contracts,
        "expected_episode_indexes": [int(item["index"]) for item in episodes],
        "submission_policy": {
            "coordinator_only": True,
            "require_all_episodes": True,
            "require_all_subtitle_indexes": True,
            "preserve_timestamps_exactly": True,
            "incomplete_submission_action": "reject_and_continue_waiting",
        },
        "episodes": episodes,
    }
    job_dir, request = agent_bridge.create_job(bridge_root, payload)
    job_id = request["job_id"]
    print(f"[Agent 阶段 2/3] 已提交文件夹任务 {job_id}，等待已注册对话处理")
    try:
        response = agent_bridge.wait_for_response(
            bridge_root,
            job_id,
            timeout_seconds=float(getattr(args, "agent_wait_timeout_seconds", 0) or 0),
        )
        quality_gate = agent_bridge.validate_agent_quality_gate(
            response, minimum_score=9.5 if quality_mode == "advanced" else 8.5
        )
        expected = {int(item["index"]) for item in prepared}
        response_episodes = response.get("episodes")
        if not isinstance(response_episodes, list):
            raise RuntimeError("Agent response is missing episodes")
        received = {int(item.get("index", -1)) for item in response_episodes if isinstance(item, dict)}
        if received != expected or len(response_episodes) != len(expected):
            raise RuntimeError(f"Agent episode indexes mismatch: expected={sorted(expected)}, received={sorted(received)}")
        if str(response.get("target_language", "")).casefold() != str(args.target_language).casefold():
            raise RuntimeError("Agent response target language does not match the task")
        prepared_by_index = {int(item["index"]): item for item in prepared}
        request_episode_by_index = {int(item["index"]): item for item in episodes}
        for episode in response_episodes:
            episode_index = int(episode["index"])
            rows = episode.get("subtitles")
            if not isinstance(rows, list) or not rows:
                raise RuntimeError(f"Agent episode {episode_index} has no subtitles")
            source_rows = episode.get("source_subtitles")
            if not isinstance(source_rows, list) or not source_rows:
                raise RuntimeError(f"Agent episode {episode_index} has no repaired source subtitles")
            expected_rows = request_episode_by_index[episode_index]["items"]
            expected_indexes = [int(row["index"]) for row in expected_rows]
            received_indexes = [int(row.get("index", -1)) for row in rows if isinstance(row, dict)]
            if received_indexes != expected_indexes:
                raise RuntimeError(
                    f"Agent episode {episode_index} subtitle indexes mismatch: "
                    f"expected={expected_indexes[:30]}, received={received_indexes[:30]}"
                )
            received_source_indexes = [
                int(row.get("index", -1)) for row in source_rows if isinstance(row, dict)
            ]
            if received_source_indexes != expected_indexes:
                raise RuntimeError(
                    f"Agent episode {episode_index} repaired source indexes mismatch: "
                    f"expected={expected_indexes[:30]}, received={received_source_indexes[:30]}"
                )
            output_items = []
            for position, (row, expected_row) in enumerate(zip(rows, expected_rows), 1):
                if not isinstance(row, dict) or not isinstance(row.get("text"), str):
                    raise RuntimeError(f"Agent episode {episode_index} subtitle {position} is invalid")
                start = str(row.get("start", ""))
                end = str(row.get("end", ""))
                if start != str(expected_row["start"]) or end != str(expected_row["end"]):
                    raise RuntimeError(f"Agent episode {episode_index} subtitle {position} changed its timing")
                if subtitle_tool.srt_time_to_seconds(end) <= subtitle_tool.srt_time_to_seconds(start):
                    raise RuntimeError(f"Agent episode {episode_index} subtitle {position} has invalid timing")
                normalized_text = subtitle_tool.normalize_target_language_text(
                    str(row["text"]), args.target_language
                )
                # Keep the archived Agent response consistent with the SRT
                # that the video pipeline actually materializes.
                row["text"] = normalized_text
                output_items.append(
                    subtitle_tool.SubtitleItem(
                        int(row["index"]),
                        start,
                        end,
                        normalized_text,
                    )
                )
            subtitle_tool.write_srt(output_items, Path(prepared_by_index[episode_index]["translated_srt"]))
            source_language = request_episode_by_index[episode_index]["source_output"]["language"]
            source_output_items = []
            for position, (row, expected_row) in enumerate(zip(source_rows, expected_rows), 1):
                if not isinstance(row, dict) or not isinstance(row.get("text"), str):
                    raise RuntimeError(f"Agent episode {episode_index} repaired source {position} is invalid")
                start = str(row.get("start", ""))
                end = str(row.get("end", ""))
                if start != str(expected_row["start"]) or end != str(expected_row["end"]):
                    raise RuntimeError(
                        f"Agent episode {episode_index} repaired source {position} changed its timing"
                    )
                source_output_items.append(
                    subtitle_tool.SubtitleItem(
                        int(row["index"]),
                        start,
                        end,
                        subtitle_tool.normalize_target_language_text(str(row["text"]), source_language),
                    )
                )
            subtitle_tool.write_srt(
                source_output_items,
                Path(prepared_by_index[episode_index]["repaired_source_srt"]),
            )

        series_entities = response.get("series_entities")
        if not isinstance(series_entities, list):
            raise RuntimeError("Agent response is missing the series entity table")
        previous_entities = load_series_entity_table(
            Path(args.input).resolve(), args.target_language
        ).get("entities", [])
        merged_entities = merge_series_entities(previous_entities, series_entities)
        entity_path = save_series_entity_table(
            Path(args.input).resolve(), args.target_language, merged_entities, job_id
        )
        print(f"[Agent 实体表] 已更新: {entity_path}")

        run_dir = Path(args.translation_run_dir)
        archive_json = run_dir / f"{job_id}-agent.json"
        archive_md = run_dir / f"{job_id}-agent.md"
        measured_tokens = {
            "input": estimate_visible_tokens(payload),
            "output": estimate_visible_tokens(response),
            "method": "visible JSON character estimate; excludes hidden reasoning",
        }
        archive = {
            "schema_version": 1,
            "job_id": job_id,
            "status": "agent_translation_completed",
            "created_at": request.get("created_at"),
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "settings": {key: payload[key] for key in ("title", "input_directory", "output_directory", "target_language", "source_language", "ocr_language", "subtitle_source", "translation_quality", "localization_strategy", "max_parallel")},
            "glossary": payload.get("glossary"),
            "source_material": episodes,
            "result": response,
            "quality_gate": quality_gate,
            "token_estimate": measured_tokens,
        }
        agent_bridge.atomic_write_json(archive_json, archive)
        token_estimate = measured_tokens
        series_review = response.get("series_review") or {}
        advanced_review = response.get("advanced_review") or {}
        warnings = series_review.get("warnings") or []
        suggestions = response.get("glossary_suggestions") or []
        archive_md.write_text(
            "\n".join(
                [
                    f"# Agent 字幕任务报告：{payload['title']}",
                    "",
                    f"- 任务：`{job_id}`",
                    f"- 状态：Agent 翻译审核完成；本地成片流水线继续执行",
                    f"- 视频：{len(prepared)} 个",
                    f"- 目标语言：{args.target_language}",
                    f"- 翻译质量：{'高级翻译' if quality_mode == 'advanced' else '快速翻译'}",
                    f"- 本地化策略：{localization_strategy}",
                    f"- 全剧质量评分：{quality_gate['series_score']:.1f}/10（门槛 {quality_gate['threshold']:.1f}）",
                    f"- 单集最低评分：{min(quality_gate['episode_scores'].values()):.1f}/10",
                    f"- 可见输入 Token 估算：{token_estimate.get('input', '未提供')}",
                    f"- 可见输出 Token 估算：{token_estimate.get('output', '未提供')}",
                    "",
                    "## 全剧审核",
                    "",
                    str(series_review.get("summary") or "Agent 未提供文字摘要。"),
                    *(
                        [
                            "",
                            "## 高级翻译",
                            "",
                            f"- 修订循环：{advanced_review.get('revision_cycles', '未提供')}",
                            f"- 独立终审：{'是' if advanced_review.get('independent_final_review') else '否'}",
                            f"- 摘要：{advanced_review.get('summary') or '未提供'}",
                        ]
                        if quality_mode == "advanced"
                        else []
                    ),
                    "",
                    "## 警告",
                    "",
                    *(f"- {value}" for value in warnings),
                    *( [] if warnings else ["- 无"] ),
                    "",
                    "## 术语表建议",
                    "",
                    *(f"- {value}" for value in suggestions),
                    *( [] if suggestions else ["- 无"] ),
                ]
            ),
            encoding="utf-8",
        )
        print(
            "[Agent 审核报告] "
            f"{series_review.get('summary') or '未提供摘要'} | "
            f"警告={len(warnings)} | 术语建议={len(suggestions)} | "
            f"质量={quality_gate['series_score']:.1f}/10 | "
            f"模式={quality_mode} | 实体={len(merged_entities)} | "
            f"可见Token估算={measured_tokens['input']}输入/{measured_tokens['output']}输出"
        )
        print(f"[Agent 阶段 3/3] 返回通过校验，诊断记录: {archive_json} / {archive_md}")
        return archive_json, archive_md
    finally:
        agent_bridge.cleanup_job(job_dir)


def prepare_video_subtitles(
    args: argparse.Namespace,
    input_video: Path,
    output_video: Path,
    seed: int | None,
    index: int,
    total: int,
) -> dict:
    """Extract and translate one video without starting irreversible encoding."""
    progress = f"[视频 {index}/{total}]"

    def log(message: str) -> None:
        print(f"{progress} {message}")

    ffmpeg = video_dedup.find_binary("ffmpeg", args.ffmpeg)
    ffprobe = video_dedup.find_binary("ffprobe", args.ffprobe)
    work_dir = Path(args.translation_run_dir) / "_working" / f"{index:03d}-{safe_record_stem(input_video.stem)}"
    work_dir.mkdir(parents=True, exist_ok=True)
    visual_srt = work_dir / "visual_source.srt"
    asr_srt = work_dir / "audio_asr_source.srt"
    repaired_source_srt = work_dir / "repaired_source.srt"
    translated_srt = work_dir / "translated.srt"
    translation_record_path = Path(args.translation_run_dir) / f"{index:03d}-{safe_record_stem(input_video.stem)}.json"
    record_context = {
        "input": str(input_video.resolve()),
        "output": str(output_video.resolve()),
        "index": index,
        "total": total,
    }
    log(f"翻译诊断记录: {translation_record_path}")
    log("[翻译阶段 1/2] 获取字幕来源")
    sources = None
    for source_attempt in range(1, 4):
        try:
            sources = make_subtitle_sources(
                input_video,
                visual_srt,
                asr_srt,
                args.subtitle_source,
                resolved_ocr_language(args.ocr_language, args.source_language),
                args.source_language,
                args.whisper_model,
                args.whisper_device,
                ffmpeg,
                ffprobe,
                args.ocr_timeout_seconds,
                args.asr_timeout_seconds,
                progress,
                ocr_device=args.ocr_device,
                global_asr_workers=getattr(args, "global_asr_workers", 5),
            )
            break
        except RuntimeError as exc:
            if "OCR 与音频 ASR 均失败" not in str(exc) or source_attempt >= 3:
                raise
            delay = 2 ** source_attempt
            log(f"OCR 与 ASR 同时失败，{delay} 秒后重新执行字幕来源阶段 ({source_attempt}/3)")
            visual_srt.unlink(missing_ok=True)
            asr_srt.unlink(missing_ok=True)
            asr_srt.with_suffix(".words.json").unlink(missing_ok=True)
            time.sleep(delay)
    if sources is None:
        raise RuntimeError("字幕来源阶段未返回结果")
    log(f"[翻译阶段 1/2] 完成，来源={'+'.join(sources)}")
    visual_kind = "soft" if "soft" in sources else "ocr"
    visual_path = sources.get("soft") or sources.get("ocr")
    audio_path = sources.get("asr")
    audio_words_path = sources.get("asr_words")
    log("[翻译阶段 2/2] LLM 初译与整集语义审核")
    if visual_path and audio_path:
        subtitle_tool.translate_dual_source_srts(
            visual_path,
            audio_path,
            translated_srt,
            args.target_language,
            _ocr_language_name(args.ocr_language, args.source_language),
            args.source_language,
            args.llm_model,
            args.enable_llm_review,
            args.llm_model_b,
            args.llm_review_model,
            visual_kind,
            audio_words_path,
            args.review_confidence_threshold,
            translation_record_path,
            record_context,
            args.glossary_data,
            getattr(args, "localization_instruction", LOCALIZATION_INSTRUCTIONS["cinematic_standard"]),
        )
    else:
        source_kind, source_srt = next(
            (key, value) for key, value in sources.items() if key != "asr_words"
        )
        subtitle_tool.translate_srt(
            source_srt,
            translated_srt,
            args.target_language,
            _ocr_language_name(args.ocr_language, args.source_language)
            if source_kind == "ocr"
            else args.source_language,
            "openai-compatible",
            args.llm_model,
            args.parallel_batches,
            args.enable_llm_review,
            args.llm_model_b,
            args.llm_review_model,
            source_kind,
            translation_record_path,
            record_context,
            args.glossary_data,
            getattr(args, "localization_instruction", LOCALIZATION_INSTRUCTIONS["cinematic_standard"]),
        )
    source_item = {
        "visual_kind": visual_kind,
        "visual_path": visual_path,
        "audio_path": audio_path,
        "audio_words_path": audio_words_path,
    }
    write_repaired_source_from_evidence(args, source_item, repaired_source_srt)
    log("[翻译阶段 2/2] 完成，等待全剧一致性审核")
    return {
        "index": index,
        "total": total,
        "input_video": input_video,
        "output_video": output_video,
        "seed": seed,
        "translated_srt": translated_srt,
        "repaired_source_srt": repaired_source_srt,
        "source_repair_method": "aligned_evidence_cleanup",
        "visual_kind": visual_kind,
        "translation_record_path": translation_record_path,
    }


def encode_prepared_subtitle_video(args: argparse.Namespace, prepared: dict) -> None:
    index = int(prepared["index"])
    total = int(prepared["total"])
    input_video = Path(prepared["input_video"])
    output_video = Path(prepared["output_video"])
    seed = prepared["seed"]
    translated_srt = Path(prepared["translated_srt"])
    progress = f"[视频 {index}/{total}]"

    def log(message: str) -> None:
        print(f"{progress} {message}")

    started = time.perf_counter()
    output_video.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = video_dedup.find_binary("ffmpeg", args.ffmpeg)
    ffprobe = video_dedup.find_binary("ffprobe", args.ffprobe)
    with tempfile.TemporaryDirectory(prefix="video-pipeline-") as temp_name:
        temp = Path(temp_name)
        config_path = sanitize_video_config(Path(args.config).resolve(), temp / "video_config.json")
        config = video_dedup.load_config(args.preset, str(config_path), seed)
        config = video_dedup.choose_background_music(config, seed)
        transformed = temp / f"{input_video.stem}_dedup{output_video.suffix or input_video.suffix}"
        timed_srt = temp / "translated_timed.srt"
        log("[成片阶段 1/3] 调整字幕时间轴")
        adjusted_subtitle_for_transform(translated_srt, timed_srt, input_video, config, ffprobe)
        log("[成片阶段 2/3] 视频去重编码")
        run_video_transform(
            input_video,
            transformed,
            args.preset,
            config_path,
            seed,
            args.hardware_acceleration,
            ffmpeg,
            ffprobe,
        )
        log("[成片阶段 3/3] 写入全剧审核后的字幕")
        subtitle_tool.render_subtitle(
            transformed,
            timed_srt,
            output_video,
            args.subtitle_mode,
            args.subtitle_layout,
            args.subtitle_position,
            args.subtitle_cover,
            args.cover_x_percent,
            args.cover_y_percent,
            args.cover_width_percent,
            args.cover_height_percent,
            args.cover_opacity,
            args.cover_color,
            args.cover_auto_detect,
            args.ocr_language,
            args.font_name,
            args.font_size,
            config.crf,
            args.hardware_acceleration,
            ffmpeg,
            dry_run=False,
            cover_mode=args.cover_mode,
            cover_blur_sigma=args.cover_blur_sigma,
        )
    log(f"完成，用时 {time.perf_counter() - started:.1f}s: {output_video}")


def process_video(args: argparse.Namespace, input_video: Path, output_video: Path, seed: int | None, index: int, total: int) -> None:
    progress = f"[视频 {index}/{total}]"

    def log(message: str) -> None:
        print(f"{progress} {message}")

    output_video.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = video_dedup.find_binary("ffmpeg", args.ffmpeg)
    ffprobe = video_dedup.find_binary("ffprobe", args.ffprobe)

    video_started = time.perf_counter()
    log(f"开始: {input_video.name} -> {output_video.name}")
    translation_record_path = None
    record_context = {
        "input": str(input_video.resolve()),
        "output": str(output_video.resolve()),
        "index": index,
        "total": total,
    }
    if args.enable_subtitles and getattr(args, "translation_run_dir", None):
        translation_record_path = Path(args.translation_run_dir) / f"{index:03d}-{safe_record_stem(input_video.stem)}.json"
        log(f"翻译诊断记录: {translation_record_path}")
    with tempfile.TemporaryDirectory(prefix="video-pipeline-") as temp_name:
        temp = Path(temp_name)
        config_path = sanitize_video_config(Path(args.config).resolve(), temp / "video_config.json")
        config = video_dedup.load_config(args.preset, str(config_path), seed)
        config = video_dedup.choose_background_music(config, seed)
        transformed = temp / f"{input_video.stem}_dedup{output_video.suffix or input_video.suffix}"
        visual_srt = temp / "visual_source.srt"
        asr_srt = temp / "audio_asr_source.srt"
        translated_srt = temp / "translated.srt"
        timed_srt = temp / "translated_timed.srt"

        if args.enable_subtitles:
            log("[阶段 1/5] 获取字幕来源")
            sources = make_subtitle_sources(
                input_video,
                visual_srt,
                asr_srt,
                args.subtitle_source,
                resolved_ocr_language(args.ocr_language, args.source_language),
                args.source_language,
                args.whisper_model,
                args.whisper_device,
                ffmpeg,
                ffprobe,
                args.ocr_timeout_seconds,
                args.asr_timeout_seconds,
                progress,
                ocr_device=args.ocr_device,
                global_asr_workers=getattr(args, "global_asr_workers", 5),
            )
            log(f"[阶段 1/5] 完成，来源={'+'.join(sources)}")
            visual_kind = "soft" if "soft" in sources else "ocr"
            visual_path = sources.get("soft") or sources.get("ocr")
            audio_path = sources.get("asr")
            audio_words_path = sources.get("asr_words")
            log("[阶段 2/5] LLM 翻译与审核")
            if visual_path and audio_path:
                log(f"双源时间轴对齐并翻译: {visual_kind} + asr")
                subtitle_tool.translate_dual_source_srts(
                    visual_path,
                    audio_path,
                    translated_srt,
                    args.target_language,
                    _ocr_language_name(args.ocr_language, args.source_language),
                    args.source_language,
                    args.llm_model,
                    args.enable_llm_review,
                    args.llm_model_b,
                    args.llm_review_model,
                    visual_kind,
                    audio_words_path,
                    args.review_confidence_threshold,
                    translation_record_path,
                    record_context,
                    args.glossary_data,
                    getattr(args, "localization_instruction", LOCALIZATION_INSTRUCTIONS["cinematic_standard"]),
                )
            else:
                source_kind, source_srt = next(iter(sources.items()))
                log(f"单源降级翻译: {source_kind}")
                subtitle_tool.translate_srt(
                    source_srt,
                    translated_srt,
                    args.target_language,
                    _ocr_language_name(args.ocr_language, args.source_language) if source_kind == "ocr" else args.source_language,
                    "openai-compatible",
                    args.llm_model,
                    args.parallel_batches,
                    args.enable_llm_review,
                    args.llm_model_b,
                    args.llm_review_model,
                    source_kind,
                    translation_record_path,
                    record_context,
                    args.glossary_data,
                    getattr(args, "localization_instruction", LOCALIZATION_INSTRUCTIONS["cinematic_standard"]),
                )
            log("[阶段 2/5] LLM 翻译与审核完成")
            log("[阶段 3/5] 调整字幕时间轴")
            adjusted_subtitle_for_transform(translated_srt, timed_srt, input_video, config, ffprobe)
            log("[阶段 3/5] 字幕时间轴完成")

        log("[阶段 4/5] 视频去重编码" if args.enable_subtitles else "[阶段 1/1] 视频去重编码")
        run_video_transform(
            input_video,
            transformed,
            args.preset,
            config_path,
            seed,
            args.hardware_acceleration,
            ffmpeg,
            ffprobe,
        )
        log("[阶段 4/5] 视频去重编码完成" if args.enable_subtitles else "[阶段 1/1] 视频去重编码完成")

        if not args.enable_subtitles:
            shutil.copy2(transformed, output_video)
            log(f"完成，用时 {time.perf_counter() - video_started:.1f}s: {output_video}")
            return

        log(
            "[阶段 5/5] 写入字幕到去重后视频 "
            f"(mode={args.subtitle_mode}, layout={args.subtitle_layout}, cover={args.subtitle_cover}, "
            f"font={args.font_name}, size={args.font_size})"
        )
        subtitle_tool.render_subtitle(
            transformed,
            timed_srt,
            output_video,
            args.subtitle_mode,
            args.subtitle_layout,
            args.subtitle_position,
            args.subtitle_cover,
            args.cover_x_percent,
            args.cover_y_percent,
            args.cover_width_percent,
            args.cover_height_percent,
            args.cover_opacity,
            args.cover_color,
            args.cover_auto_detect,
            args.ocr_language,
            args.font_name,
            args.font_size,
            config.crf,
            args.hardware_acceleration,
            ffmpeg,
            dry_run=False,
            cover_mode=args.cover_mode,
            cover_blur_sigma=args.cover_blur_sigma,
        )
        log(f"[阶段 5/5] 字幕写入完成")
        log(f"完成，用时 {time.perf_counter() - video_started:.1f}s: {output_video}")


def process(args: argparse.Namespace) -> int:
    if getattr(args, "subtitle_only", False) and not args.enable_subtitles:
        raise ValueError("--subtitle-only 必须与 --enable-subtitles 一起使用")
    if not 0.0 <= args.review_confidence_threshold <= 1.0:
        raise ValueError("review_confidence_threshold 必须在 0 到 1 之间")
    if int(getattr(args, "global_asr_workers", 5)) < 1:
        raise ValueError("global_asr_workers 必须至少为 1")
    args.localization_instruction = LOCALIZATION_INSTRUCTIONS.get(
        getattr(args, "localization_strategy", "cinematic_standard"),
        LOCALIZATION_INSTRUCTIONS["cinematic_standard"],
    )
    if getattr(args, "translation_quality", "fast") == "advanced" and getattr(args, "translation_backend", "api") != "agent":
        raise ValueError("高级翻译需要 --translation-backend agent；API 模式仅支持快速翻译")
    glossary_file = getattr(args, "glossary_file", None)
    args.glossary_data = subtitle_tool.load_glossary_file(Path(glossary_file)) if glossary_file else None
    if args.glossary_data:
        print(
            f"已加载术语表: {args.glossary_data['name']} "
            f"({len(args.glossary_data['terms'])} 条) -> {args.glossary_data['_source_path']}"
        )
    inputs = collect_pipeline_inputs(Path(args.input), Path(args.input_list).resolve() if args.input_list else None)
    if not inputs:
        raise ValueError("没有找到可处理的视频")

    if args.enable_subtitles:
        record_root = (
            Path(args.translation_log_dir).resolve()
            if args.translation_log_dir
            else Path(__file__).resolve().parent / "logs" / "translation-records"
        )
        run_name = f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}"
        args.translation_run_dir = record_root / run_name
        args.translation_run_dir.mkdir(parents=True, exist_ok=True)
        print(f"本次翻译诊断目录: {args.translation_run_dir}")
    else:
        args.translation_run_dir = None

    output = Path(args.output).resolve()
    output_is_file = len(inputs) == 1 and output.suffix.lower() in video_dedup.VIDEO_SUFFIXES
    if not getattr(args, "subtitle_only", False):
        if output_is_file:
            output.parent.mkdir(parents=True, exist_ok=True)
        else:
            output.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[int, Path, Path, int | None]] = []
    for index, input_video in enumerate(inputs, 1):
        output_video = output if output_is_file else output / f"{input_video.stem}_local{input_video.suffix}"
        if input_video.resolve() == output_video.resolve():
            raise ValueError("输出文件不能覆盖输入文件")
        seed = args.seed + index - 1 if args.seed is not None and len(inputs) > 1 else args.seed
        jobs.append((index, input_video, output_video, seed))

    if not getattr(args, "subtitle_only", False) and getattr(args, "checkpoint_dir", None):
        pending_jobs = []
        for index, input_video, output_video, seed in jobs:
            if completed_batch_checkpoint(args, input_video, output_video, index):
                print(f"[断点续做] 视频 {index}/{len(inputs)} 已完成且校验通过，跳过: {input_video.name}")
            else:
                pending_jobs.append((index, input_video, output_video, seed))
        jobs = pending_jobs
        if not jobs:
            print("[断点续做] 本模块所有视频均已完成，无需重复编码。")
            return 0

    workers = max(1, min(int(args.video_workers), len(jobs)))
    print(
        f"批量任务开始: videos={len(jobs)}, video_workers={workers}, subtitles={args.enable_subtitles}, "
        f"subtitle_only={getattr(args, 'subtitle_only', False)}, "
        f"source={args.subtitle_source}, target={args.target_language}"
    )
    if args.enable_subtitles:
        print("目录字幕流水线: 并发提取/初译/整集审核 → 全剧一致性审核 → 并发编码")
        agent_mode = getattr(args, "translation_backend", "api") == "agent"
        prepare_function = prepare_video_sources_for_agent if agent_mode else prepare_video_subtitles
        if agent_mode:
            print("Agent mode: local OCR/ASR extraction -> folder-level Agent review -> parallel encoding")
        prepared_by_index: dict[int, dict] = {}

        def prepare_or_load_cache(
            input_video: Path,
            output_video: Path,
            seed: int | None,
            index: int,
        ) -> dict:
            cached = load_final_subtitle_cache(
                args, input_video, output_video, seed, index, len(inputs)
            )
            if cached is not None:
                return cached
            result = prepare_function(args, input_video, output_video, seed, index, len(inputs))
            result["final_subtitle_cache_hit"] = False
            return result

        if workers == 1:
            for index, input_video, output_video, seed in jobs:
                prepared_by_index[index] = prepare_or_load_cache(input_video, output_video, seed, index)
                print(f"[翻译总进度 {len(prepared_by_index)}/{len(jobs)}] 视频 {index} 已准备")
        else:
            print(f"字幕并发准备: workers={workers}, videos={len(jobs)}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="subtitle-worker") as executor:
                futures = {
                    executor.submit(
                        prepare_or_load_cache, input_video, output_video, seed, index
                    ): index
                    for index, input_video, output_video, seed in jobs
                }
                for future in concurrent.futures.as_completed(futures):
                    index = futures[future]
                    try:
                        prepared_by_index[index] = future.result()
                        print(f"[翻译总进度 {len(prepared_by_index)}/{len(jobs)}] 视频 {index} 已准备")
                    except Exception as exc:
                        print(f"[视频 {index}/{len(inputs)}] 翻译准备错误: {exc}")
                        raise

        prepared = [prepared_by_index[index] for index, *_rest in jobs]
        newly_translated = [item for item in prepared if not item.get("final_subtitle_cache_hit")]
        cacheable_subtitles = newly_translated
        cache_hits = len(prepared) - len(newly_translated)
        if cache_hits:
            print(f"[字幕终稿] 命中 {cache_hits}/{len(prepared)} 个视频；仅处理剩余 {len(newly_translated)} 个")
        if agent_mode:
            if newly_translated:
                run_agent_translation(args, newly_translated)
            else:
                print("[Agent] 全部视频均命中字幕终稿，跳过文件桥接任务")
        elif args.enable_llm_review and newly_translated:
            print("[全剧审核] 开始统一人物、家族、地点、组织、称谓和头衔")
            report_path = Path(args.translation_run_dir) / "series-consistency.json"
            try:
                subtitle_tool.review_series_consistency_openai_compatible(
                    [Path(item["translation_record_path"]) for item in newly_translated],
                    [Path(item["translated_srt"]) for item in newly_translated],
                    args.target_language,
                    args.llm_review_model.strip() or args.llm_model,
                    report_path,
                )
            except Exception as exc:
                cacheable_subtitles = []
                print(f"[全剧审核] 失败，保留各集整集审核结果继续编码: {exc}", file=sys.stderr)
                subtitle_tool.write_translation_record(
                    report_path,
                    {
                        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "status": "failed",
                        "model": args.llm_review_model.strip() or args.llm_model,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                )
        else:
            print("[全剧审核] 未启用审核模型，跳过一致性审核。")

        # Cache the reviewed subtitle on the original source timeline. Existing
        # trim/speed adjustment happens later during encode.
        save_final_subtitle_caches(args, cacheable_subtitles)

        if getattr(args, "subtitle_only", False):
            print(
                f"[字幕阶段完成] 已生成/复用 {len(prepared)} 个字幕终稿；"
                "未执行视频去重、字幕烧录或视频编码。"
            )
            shutil.rmtree(Path(args.translation_run_dir) / "_working", ignore_errors=True)
            return 0

        completed = 0
        if workers == 1:
            for item in prepared:
                encode_prepared_subtitle_video(args, item)
                save_batch_checkpoint(
                    args, Path(item["input_video"]), Path(item["output_video"]), int(item["index"])
                )
                completed += 1
                print(f"[总进度 {completed}/{len(jobs)}] 视频 {item['index']} 已完成")
        else:
            print(f"成片并发处理: workers={workers}, videos={len(jobs)}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="encode-worker") as executor:
                futures = {executor.submit(encode_prepared_subtitle_video, args, item): item["index"] for item in prepared}
                for future in concurrent.futures.as_completed(futures):
                    index = futures[future]
                    try:
                        future.result()
                        item = next(value for value in prepared if value["index"] == index)
                        save_batch_checkpoint(
                            args, Path(item["input_video"]), Path(item["output_video"]), int(index)
                        )
                        completed += 1
                        print(f"[总进度 {completed}/{len(jobs)}] 视频 {index} 已完成")
                    except Exception as exc:
                        print(f"[视频 {index}/{len(inputs)}] 成片错误: {exc}")
                        raise
        shutil.rmtree(Path(args.translation_run_dir) / "_working", ignore_errors=True)
        return 0

    if workers == 1:
        completed = 0
        for index, input_video, output_video, seed in jobs:
            try:
                process_video(args, input_video, output_video, seed, index, len(inputs))
                save_batch_checkpoint(args, input_video, output_video, index)
                completed += 1
                print(f"[总进度 {completed}/{len(jobs)}] 已完成 {input_video.name}")
            except Exception as exc:
                print(f"[视频 {index}/{len(inputs)}] 错误: {exc}")
                raise
    else:
        print(f"视频并发处理: workers={workers}, videos={len(jobs)}")
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="video-worker") as executor:
            futures = {
                executor.submit(process_video, args, input_video, output_video, seed, index, len(inputs)): index
                for index, input_video, output_video, seed in jobs
            }
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                try:
                    future.result()
                    input_video, output_video = next(
                        (source, target)
                        for job_index, source, target, _seed in jobs
                        if job_index == index
                    )
                    save_batch_checkpoint(args, input_video, output_video, index)
                    completed += 1
                    print(f"[总进度 {completed}/{len(jobs)}] 视频 {index} 已完成")
                except Exception as exc:
                    print(f"[视频 {index}/{len(inputs)}] 错误: {exc}")
                    raise
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="目录/文件完整视频处理流水线")
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--input-list", help="JSON 文件路径数组；用于把多选文件作为一个任务组")
    parser.add_argument("--preset", choices=video_dedup.PRESETS, default="medium")
    parser.add_argument("--config", required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--hardware-acceleration", choices=("auto", "nvidia", "amd", "intel", "apple", "cpu"), default="nvidia")
    parser.add_argument("--enable-subtitles", action="store_true")
    parser.add_argument(
        "--subtitle-only",
        action="store_true",
        help="只生成或复用源目录下的字幕终稿，不执行视频去重、字幕烧录或视频编码",
    )
    parser.add_argument(
        "--force-subtitle-translation",
        "--no-final-subtitle-cache",
        dest="no_final_subtitle_cache",
        action="store_true",
        help="忽略源目录的字幕终稿并强制重新识别、翻译和审核",
    )
    parser.add_argument("--translation-backend", choices=("api", "agent"), default="api")
    parser.add_argument("--translation-quality", choices=("fast", "advanced"), default="fast")
    parser.add_argument(
        "--localization-strategy",
        choices=tuple(LOCALIZATION_INSTRUCTIONS),
        default="cinematic_standard",
    )
    parser.add_argument("--agent-bridge-root", default=str(Path(__file__).resolve().parent / "agent-bridge"))
    parser.add_argument("--agent-wait-timeout-seconds", type=float, default=0.0, help="0 means wait until Agent reply or cancellation")
    parser.add_argument("--agent-task-title", default="")
    parser.add_argument(
        "--subtitle-source",
        choices=("auto", "auto-ocr", "soft-asr", "ocr-asr", "soft", "hard-ocr", "asr"),
        default="hard-ocr",
    )
    parser.add_argument("--target-language", default="English")
    parser.add_argument("--source-language", default="auto")
    parser.add_argument("--ocr-language", default="auto")
    parser.add_argument("--ocr-device", choices=("auto", "cuda", "cpu"), default="cuda")
    parser.add_argument("--llm-model", default="deepseek-v4-flash")
    parser.add_argument("--enable-llm-review", action="store_true", help="启用整集语义审核与目录级全剧一致性审核")
    parser.add_argument("--llm-model-b", default="", help="兼容旧命令，智能审核流程不再使用第二翻译模型")
    parser.add_argument("--llm-review-model", default="", help="风险字幕审核模型，留空则复用 --llm-model")
    parser.add_argument("--glossary-file", help="可选 JSON 术语表；同时用于初译和审核")
    parser.add_argument("--translation-log-dir", help="翻译诊断记录根目录；默认保存到项目 logs/translation-records")
    parser.add_argument(
        "--checkpoint-dir",
        help="可选的逐视频完成检查点目录；仅在输出文件和配置签名同时匹配时跳过",
    )
    parser.add_argument(
        "--review-confidence-threshold",
        type=float,
        default=0.82,
        help="风险诊断阈值，范围 0 到 1；整集审核不再据此跳过字幕",
    )
    parser.add_argument("--parallel-batches", type=int, default=1, help="兼容旧命令；当前每个视频固定只发送一个翻译请求")
    parser.add_argument("--video-workers", type=int, default=1, help="同一个目录/文件组内同时处理的视频数量")
    parser.add_argument("--ocr-timeout-seconds", type=int, default=600, help="单视频 OCR 超时；超时后降级使用 ASR")
    parser.add_argument("--asr-timeout-seconds", type=int, default=600, help="单视频 ASR 超时；超时后降级使用 OCR")
    parser.add_argument("--global-asr-workers", type=int, default=5, help="所有文件夹任务共享的 ASR 并发槽位，默认 5")
    parser.add_argument("--whisper-model", default="medium")
    parser.add_argument("--whisper-device", choices=("auto", "cuda", "cpu"), default="cuda")
    parser.add_argument("--subtitle-mode", choices=("burn", "soft"), default="burn")
    parser.add_argument("--subtitle-layout", choices=("replace", "bilingual"), default="replace")
    parser.add_argument("--subtitle-position", choices=("auto", "bottom", "above-original", "top"), default="auto")
    parser.add_argument("--subtitle-cover", action="store_true")
    parser.add_argument("--cover-auto-detect", action="store_true")
    parser.add_argument("--cover-x-percent", type=float, default=0.0)
    parser.add_argument("--cover-y-percent", type=float, default=74.0)
    parser.add_argument("--cover-width-percent", type=float, default=100.0)
    parser.add_argument("--cover-height-percent", type=float, default=11.0)
    parser.add_argument("--cover-opacity", type=float, default=0.82)
    parser.add_argument("--cover-color", default="white")
    parser.add_argument("--cover-mode", choices=("blur", "color"), default="blur")
    parser.add_argument("--cover-blur-sigma", type=float, default=22.0)
    parser.add_argument("--font-name", default="Arial")
    parser.add_argument("--font-size", type=int, default=28)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    video_dedup.install_hidden_subprocess_policy()
    args = make_parser().parse_args(argv)
    try:
        return process(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        run_dir = getattr(args, "translation_run_dir", None)
        if run_dir:
            shutil.rmtree(Path(run_dir) / "_working", ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
