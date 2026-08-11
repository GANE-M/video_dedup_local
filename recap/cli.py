from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import video_dedup

from .project_store import (
    create_project, delete_segment, diff_versions, load_project, rollback_project,
    save_project, update_segment,
)
from .pacing import get_preset, normalize_preset
from .renderer import generate_voice_preview, inspect_sources, measure_project_loudness, render_project
from .timeline import validate_source_intervals
from .tts_routing import resolve_tts_engine
from .voice_library import VoiceLibrary


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def default_project_payload(args: argparse.Namespace, target_duration: float) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": args.project_id,
        "project_name": args.project_name or args.project_id,
        "source_root": str(Path(args.source).resolve()),
        "episode_pattern": args.episode_pattern,
        "subtitle_root": str(Path(args.subtitle_root).resolve()) if args.subtitle_root else "",
        "output_root": str(Path(args.output).resolve()),
        "target_language": args.target_language,
        "target_duration_seconds": target_duration,
        "narration_preset": args.narration_preset,
        "voice_id": args.voice_id,
        "tts_engine": args.tts_engine,
        "narration_speed": args.narration_speed,
        "narration_target_loudness": "keep_original",
        "segments": [],
        "current_version": 1,
        "rendering": {
            "hardware_acceleration": "nvidia", "crf": 23, "caption_y_percent": 12.0,
            "caption_font_size": 38,
            "target_duration_ratio": args.target_ratio,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recap", description="Short-drama recap project and rendering CLI")
    parser.add_argument("--ffmpeg", default="")
    parser.add_argument("--ffprobe", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect-sources")
    inspect.add_argument("--source", required=True, type=Path)
    inspect.add_argument("--pattern", default="*.mp4")

    create = sub.add_parser("create-project")
    create.add_argument("--project", required=True, type=Path)
    create.add_argument("--project-id", required=True)
    create.add_argument("--project-name", default="")
    create.add_argument("--source", required=True, type=Path)
    create.add_argument("--episode-pattern", default="*第{episode}集.mp4")
    create.add_argument("--subtitle-root", default="")
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--target-language", default="English")
    create.add_argument("--target-duration", type=float, default=None, help="Explicit target seconds; overrides --target-ratio")
    create.add_argument("--target-ratio", type=float, default=None, help="Target/source duration ratio; overrides the narration preset ratio")
    create.add_argument("--narration-preset", choices=("fast", "standard", "immersive"), default="standard")
    create.add_argument("--voice-id", default="calm_female")
    create.add_argument("--tts-engine", choices=("auto", "qwen3_tts", "fish_s2", "chatterbox_v3"), default="auto")
    create.add_argument("--narration-speed", type=float, default=1.0)

    for name in ("validate-project", "measure-loudness", "render-preview", "render-final"):
        command = sub.add_parser(name)
        command.add_argument("--project", required=True, type=Path)
    segment = sub.add_parser("render-segment")
    segment.add_argument("--project", required=True, type=Path)
    segment.add_argument("--segment-id", required=True)

    voices = sub.add_parser("list-voices")
    voices.add_argument("--library", type=Path)
    preview = sub.add_parser("preview-voice")
    preview.add_argument("--voice-id", required=True)
    preview.add_argument("--library", type=Path)

    update = sub.add_parser("update-segment")
    update.add_argument("--project", required=True, type=Path)
    update.add_argument("--segment-id", required=True)
    update.add_argument("--changes-json", required=True)
    delete = sub.add_parser("delete-segment")
    delete.add_argument("--project", required=True, type=Path)
    delete.add_argument("--segment-id", required=True)

    diff = sub.add_parser("project-diff")
    diff.add_argument("--project", required=True, type=Path)
    diff.add_argument("--from", dest="from_version", required=True, type=int)
    diff.add_argument("--to", dest="to_version", required=True, type=int)
    rollback = sub.add_parser("rollback-project")
    rollback.add_argument("--project", required=True, type=Path)
    rollback.add_argument("--version", required=True, type=int)
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    ffmpeg = video_dedup.find_binary("ffmpeg", args.ffmpeg or None)
    ffprobe = video_dedup.find_binary("ffprobe", args.ffprobe or None)
    if args.command == "inspect-sources":
        return inspect_sources(args.source, ffprobe, args.pattern)
    if args.command == "create-project":
        args.narration_preset = normalize_preset(args.narration_preset)
        if args.target_ratio is None:
            args.target_ratio = get_preset(args.narration_preset).target_ratio
        if not 0 < args.target_ratio <= 1:
            raise ValueError("target ratio must be greater than 0 and no more than 1")
        source_total = None
        target_duration = args.target_duration
        if target_duration is None:
            source_pattern = args.episode_pattern.replace("{episode}", "*").replace("{number}", "*")
            source_info = inspect_sources(args.source, ffprobe, source_pattern)
            source_total = float(source_info["total_duration"])
            if source_info["episode_count"] == 0 or source_total <= 0:
                raise ValueError(f"no source videos matched episode pattern: {args.episode_pattern}")
            target_duration = round(source_total * args.target_ratio, 3)
        if target_duration <= 0:
            raise ValueError("target duration must be greater than 0")
        resolve_tts_engine(args.target_language, args.tts_engine)
        project = create_project(args.project, default_project_payload(args, target_duration))
        return {
            "status": "ok", "project_id": project.project_id, "version": project.current_version,
            "project_path": str(args.project.resolve()), "source_total_duration_seconds": source_total,
            "target_duration_ratio": args.target_ratio, "target_duration_seconds": target_duration,
        }
    if args.command == "list-voices":
        library = VoiceLibrary(args.library) if args.library else VoiceLibrary()
        return {"status": "ok", "voices": [{**item.to_dict(), "asset_errors": library.validate_assets(item.voice_id, require_preview=True)} for item in library.list()]}
    if args.command == "preview-voice":
        library = VoiceLibrary(args.library) if args.library else VoiceLibrary()
        profile = library.get(args.voice_id)
        result = generate_voice_preview(library, args.voice_id)
        return {**result, "preview_text": profile.preview_text, "warnings": library.validate_assets(profile.voice_id, require_preview=True)}
    if args.command == "update-segment":
        project, affected = update_segment(args.project, args.segment_id, json.loads(args.changes_json))
        return {"status": "ok", "project_id": project.project_id, "version": project.current_version, "affected_segments": affected}
    if args.command == "delete-segment":
        project, affected = delete_segment(args.project, args.segment_id)
        return {"status": "ok", "project_id": project.project_id, "version": project.current_version, "affected_segments": affected}
    if args.command == "project-diff":
        result = diff_versions(args.project, args.from_version, args.to_version)
        return {"status": "ok", "project_id": load_project(args.project).project_id, **result}
    if args.command == "rollback-project":
        project = rollback_project(args.project, args.version)
        return {"status": "ok", "project_id": project.project_id, "version": project.current_version, "rolled_back_from": args.version}

    project = load_project(args.project)
    if args.command == "validate-project":
        errors = validate_source_intervals(project, lambda path: video_dedup.probe_video(path, ffprobe))
        return {"status": "ok" if not errors else "validation_failed", "project_id": project.project_id, "version": project.current_version, "validation_errors": errors}
    if args.command == "measure-loudness":
        return measure_project_loudness(project, ffmpeg, ffprobe)
    if args.command == "render-segment":
        return render_project(project, only_segment_id=args.segment_id, ffmpeg=ffmpeg, ffprobe=ffprobe)
    if args.command == "render-preview":
        return render_project(project, final=False, ffmpeg=ffmpeg, ffprobe=ffprobe)
    if args.command == "render-final":
        return render_project(project, final=True, ffmpeg=ffmpeg, ffprobe=ffprobe)
    raise ValueError(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = dispatch(args)
    except Exception as exc:
        emit({"status": "error", "error_type": type(exc).__name__, "message": str(exc)})
        return 1
    emit(payload)
    return 0 if payload.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
