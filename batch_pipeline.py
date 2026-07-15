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
from global_slots import global_asr_slot


VIDEO_CONFIG_KEYS = set(video_dedup.asdict(next(iter(video_dedup.PRESETS.values()))))


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
            sys.executable, "-u", subtitle_cli, "--ffmpeg", ffmpeg, "--log-prefix", log_prefix,
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
        )
    log("[翻译阶段 2/2] 完成，等待全剧一致性审核")
    return {
        "index": index,
        "total": total,
        "input_video": input_video,
        "output_video": output_video,
        "seed": seed,
        "translated_srt": translated_srt,
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
        )
        log(f"[阶段 5/5] 字幕写入完成")
        log(f"完成，用时 {time.perf_counter() - video_started:.1f}s: {output_video}")


def process(args: argparse.Namespace) -> int:
    if not 0.0 <= args.review_confidence_threshold <= 1.0:
        raise ValueError("review_confidence_threshold 必须在 0 到 1 之间")
    if int(getattr(args, "global_asr_workers", 5)) < 1:
        raise ValueError("global_asr_workers 必须至少为 1")
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

    workers = max(1, min(int(args.video_workers), len(jobs)))
    print(
        f"批量任务开始: videos={len(jobs)}, video_workers={workers}, subtitles={args.enable_subtitles}, "
        f"source={args.subtitle_source}, target={args.target_language}"
    )
    if args.enable_subtitles:
        print("目录字幕流水线: 并发提取/初译/整集审核 → 全剧一致性审核 → 并发编码")
        prepared_by_index: dict[int, dict] = {}
        if workers == 1:
            for index, input_video, output_video, seed in jobs:
                prepared_by_index[index] = prepare_video_subtitles(
                    args, input_video, output_video, seed, index, len(inputs)
                )
                print(f"[翻译总进度 {len(prepared_by_index)}/{len(jobs)}] 视频 {index} 已准备")
        else:
            print(f"字幕并发准备: workers={workers}, videos={len(jobs)}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="subtitle-worker") as executor:
                futures = {
                    executor.submit(
                        prepare_video_subtitles, args, input_video, output_video, seed, index, len(inputs)
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
        if args.enable_llm_review:
            print("[全剧审核] 开始统一人物、家族、地点、组织、称谓和头衔")
            report_path = Path(args.translation_run_dir) / "series-consistency.json"
            try:
                subtitle_tool.review_series_consistency_openai_compatible(
                    [Path(item["translation_record_path"]) for item in prepared],
                    [Path(item["translated_srt"]) for item in prepared],
                    args.target_language,
                    args.llm_review_model.strip() or args.llm_model,
                    report_path,
                )
            except Exception as exc:
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

        completed = 0
        if workers == 1:
            for item in prepared:
                encode_prepared_subtitle_video(args, item)
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
    parser.add_argument("--hardware-acceleration", choices=("auto", "nvidia", "amd", "intel", "apple", "cpu"), default="auto")
    parser.add_argument("--enable-subtitles", action="store_true")
    parser.add_argument(
        "--subtitle-source",
        choices=("auto", "auto-ocr", "soft-asr", "ocr-asr", "soft", "hard-ocr", "asr"),
        default="hard-ocr",
    )
    parser.add_argument("--target-language", default="English")
    parser.add_argument("--source-language", default="auto")
    parser.add_argument("--ocr-language", default="auto")
    parser.add_argument("--ocr-device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--llm-model", default="deepseek-v4-flash")
    parser.add_argument("--enable-llm-review", action="store_true", help="启用整集语义审核与目录级全剧一致性审核")
    parser.add_argument("--llm-model-b", default="", help="兼容旧命令，智能审核流程不再使用第二翻译模型")
    parser.add_argument("--llm-review-model", default="", help="风险字幕审核模型，留空则复用 --llm-model")
    parser.add_argument("--glossary-file", help="可选 JSON 术语表；同时用于初译和审核")
    parser.add_argument("--translation-log-dir", help="翻译诊断记录根目录；默认保存到项目 logs/translation-records")
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
    parser.add_argument("--whisper-device", choices=("auto", "cuda", "cpu"), default="auto")
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
    parser.add_argument("--font-name", default="Arial")
    parser.add_argument("--font-size", type=int, default=28)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    video_dedup.install_hidden_subprocess_policy()
    try:
        return process(make_parser().parse_args(argv))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
