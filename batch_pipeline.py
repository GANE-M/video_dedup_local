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
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Sequence

import subtitle_tool
import video_dedup


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


def run_video_transform(input_video: Path, output_video: Path, preset: str, config: Path, seed: int | None) -> None:
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
    ]
    if seed is not None:
        command += ["--seed", str(seed)]
    print("视频去重处理:")
    print(subprocess.list2cmdline(command))
    subprocess.run(command, check=True)


def make_subtitle_source(
    input_video: Path,
    output_srt: Path,
    source_mode: str,
    whisper_model: str,
    whisper_device: str,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    streams = subtitle_tool.subtitle_streams(input_video, ffprobe)
    should_extract = source_mode == "soft" or (source_mode in {"auto", "auto-ocr"} and streams)
    if should_extract:
        if not streams:
            raise ValueError("选择了软字幕来源，但视频没有软字幕轨道。")
        print("字幕来源: 软字幕轨道")
        subtitle_tool.extract_subtitle(input_video, output_srt, 0, ffmpeg, dry_run=False)
        return
    if source_mode in {"hard-ocr", "auto", "auto-ocr"}:
        try:
            print("字幕来源: 画面硬字幕 OCR")
            subtitle_tool.ocr_hard_subtitles(input_video, output_srt, ffmpeg)
            return
        except RuntimeError as exc:
            if source_mode in {"hard-ocr", "auto-ocr"}:
                raise
            print(f"硬字幕 OCR 不可用，回退到语音识别 ASR: {exc}", file=sys.stderr)
    print("字幕来源: 语音识别 ASR")
    subtitle_tool.transcribe_video(input_video, output_srt, whisper_model, "auto", whisper_device, ffmpeg)


def adjusted_subtitle_for_transform(source_srt: Path, output_srt: Path, input_video: Path, config: video_dedup.TransformConfig, ffprobe: str) -> Path:
    info = video_dedup.probe_video(input_video, ffprobe)
    subtitle_tool.adjust_srt_timing(
        source_srt,
        output_srt,
        trim_start=config.trim_start,
        trim_end=config.trim_end,
        speed=config.speed,
        source_duration=info.duration,
    )
    return output_srt


def process_video(args: argparse.Namespace, input_video: Path, output_video: Path, seed: int | None, index: int, total: int) -> None:
    config_path = Path(args.config).resolve()
    config = video_dedup.load_config(args.preset, args.config, seed)
    config = video_dedup.choose_background_music(config, seed)
    output_video.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = video_dedup.find_binary("ffmpeg", args.ffmpeg)
    ffprobe = video_dedup.find_binary("ffprobe", args.ffprobe)

    print(f"[{index}/{total}] 开始: {input_video.name} -> {output_video.name}")
    with tempfile.TemporaryDirectory(prefix="video-pipeline-") as temp_name:
        temp = Path(temp_name)
        transformed = temp / f"{input_video.stem}_dedup{output_video.suffix or input_video.suffix}"
        source_srt = temp / "source.srt"
        translated_srt = temp / "translated.srt"
        timed_srt = temp / "translated_timed.srt"

        if args.enable_subtitles:
            make_subtitle_source(
                input_video,
                source_srt,
                args.subtitle_source,
                args.whisper_model,
                args.whisper_device,
                ffmpeg,
                ffprobe,
            )
            print("LLM 翻译字幕:")
            subtitle_tool.translate_srt(
                source_srt,
                translated_srt,
                args.target_language,
                "auto",
                "openai-compatible",
                args.llm_model,
                args.parallel_batches,
            )
            adjusted_subtitle_for_transform(translated_srt, timed_srt, input_video, config, ffprobe)

        run_video_transform(input_video, transformed, args.preset, config_path, seed)

        if not args.enable_subtitles:
            shutil.copy2(transformed, output_video)
            print(f"完成: {output_video}")
            return

        print("写入字幕到去重后视频:")
        subtitle_tool.render_subtitle(
            transformed,
            timed_srt,
            output_video,
            args.subtitle_mode,
            args.subtitle_layout,
            args.subtitle_position,
            args.subtitle_cover,
            args.cover_y_percent,
            args.cover_height_percent,
            args.cover_opacity,
            args.cover_color,
            args.cover_auto_detect,
            args.font_size,
            args.hardware_acceleration,
            ffmpeg,
            dry_run=False,
        )
        print(f"完成: {output_video}")


def process(args: argparse.Namespace) -> int:
    inputs = collect_pipeline_inputs(Path(args.input), Path(args.input_list).resolve() if args.input_list else None)
    if not inputs:
        raise ValueError("没有找到可处理的视频")

    output = Path(args.output).resolve()
    output_is_file = len(inputs) == 1 and output.suffix.lower() in video_dedup.VIDEO_SUFFIXES
    if output_is_file:
        output.parent.mkdir(parents=True, exist_ok=True)
    else:
        output.mkdir(parents=True, exist_ok=True)

    for index, input_video in enumerate(inputs, 1):
        output_video = output if output_is_file else output / f"{input_video.stem}_local{input_video.suffix}"
        if input_video.resolve() == output_video.resolve():
            raise ValueError("输出文件不能覆盖输入文件")
        seed = args.seed + index - 1 if args.seed is not None and len(inputs) > 1 else args.seed
        process_video(args, input_video, output_video, seed, index, len(inputs))
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
    parser.add_argument("--subtitle-source", choices=("auto", "auto-ocr", "soft", "hard-ocr", "asr"), default="hard-ocr")
    parser.add_argument("--target-language", default="English")
    parser.add_argument("--llm-model", default="deepseek-v4-flash")
    parser.add_argument("--parallel-batches", type=int, default=3)
    parser.add_argument("--whisper-model", default="medium")
    parser.add_argument("--whisper-device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--subtitle-mode", choices=("burn", "soft"), default="burn")
    parser.add_argument("--subtitle-layout", choices=("replace", "bilingual"), default="replace")
    parser.add_argument("--subtitle-position", choices=("auto", "bottom", "above-original", "top"), default="auto")
    parser.add_argument("--subtitle-cover", action="store_true")
    parser.add_argument("--cover-auto-detect", action="store_true")
    parser.add_argument("--cover-y-percent", type=float, default=74.0)
    parser.add_argument("--cover-height-percent", type=float, default=11.0)
    parser.add_argument("--cover-opacity", type=float, default=0.72)
    parser.add_argument("--cover-color", default="white")
    parser.add_argument("--font-size", type=int, default=28)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return process(make_parser().parse_args(argv))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
