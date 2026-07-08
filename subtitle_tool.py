#!/usr/bin/env python3
"""Local subtitle extraction, translation, replacement, and burn-in helpers."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import video_dedup


@dataclass
class SubtitleItem:
    index: int
    start: str
    end: str
    text: str


def run(command: list[str], dry_run: bool = False) -> None:
    print(subprocess.list2cmdline(command))
    if not dry_run:
        subprocess.run(command, check=True)


def subtitle_streams(video: Path, ffprobe: str) -> list[dict]:
    command = [ffprobe, "-v", "error", "-select_streams", "s", "-show_streams", "-of", "json", str(video)]
    result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
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


def seconds_to_srt_time(value: float) -> str:
    value = max(0.0, float(value))
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = int(value % 60)
    millis = int(round((value - int(value)) * 1000))
    if millis >= 1000:
        millis -= 1000
        seconds += 1
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def srt_time_to_seconds(value: str) -> float:
    match = re.match(r"^\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*$", value)
    if not match:
        raise ValueError(f"无效的 SRT 时间: {value}")
    hours, minutes, seconds, millis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")[:3]) / 1000


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


def transcribe_video(video: Path, output_srt: Path, model_size: str, language: str, device: str, ffmpeg: str) -> None:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("未安装 faster-whisper。可执行：pip install faster-whisper") from exc
    output_srt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="subtitle-audio-") as tmp:
        audio = Path(tmp) / "audio.wav"
        command = [ffmpeg, "-hide_banner", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)]
        run(command)
        compute_type = "float16" if device == "cuda" else "int8"
        print(f"加载 faster-whisper 模型: {model_size}, device={device}, compute_type={compute_type}")
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        segments, info = model.transcribe(str(audio), language=None if language == "auto" else language, vad_filter=True)
        print(f"识别语言: {info.language}, probability={info.language_probability:.2f}")
        items = []
        for idx, segment in enumerate(segments, 1):
            text = segment.text.strip()
            if text:
                items.append(SubtitleItem(idx, seconds_to_srt_time(segment.start), seconds_to_srt_time(segment.end), text))
        write_srt(items, output_srt)
        print(f"已生成字幕: {output_srt}")


def translate_texts_openai_compatible(texts: list[str], target_language: str, source_language: str, model: str) -> list[str]:
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        raise ValueError("未设置 OPENAI_API_KEY 或 LLM_API_KEY，无法自动翻译。")
    base_url = (os.environ.get("OPENAI_BASE_URL") or "https://theruta.ai/api/v1/chat/completions").rstrip("/")
    url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    print(f"AI 翻译请求: model={model}, endpoint={url}, items={len(texts)}")
    prompt = (
        "You are a professional short-drama subtitle translator. "
        "Translate each subtitle item to the target language naturally and concisely. "
        "Preserve the original meaning, speaker emotion, tone, punctuation style, and line breaks inside each item. "
        "Do not add explanations, notes, names, timestamps, numbering, or extra text. "
        "Keep the output count exactly the same as the input count and keep the same order. "
        "Return JSON only: an array of translated strings."
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {"source_language": source_language, "target_language": target_language, "items": texts},
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"翻译接口失败: HTTP {exc.code} {body}") from exc
    content = data["choices"][0]["message"]["content"].strip()
    try:
        translated = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", content)
        if not match:
            raise RuntimeError(f"翻译接口未返回 JSON 数组: {content[:300]}")
        translated = json.loads(match.group(0))
    if not isinstance(translated, list) or len(translated) != len(texts):
        raise RuntimeError("翻译结果数量与字幕数量不一致。")
    print(f"AI 翻译成功: items={len(translated)}")
    return [str(item) for item in translated]


def translate_srt(input_srt: Path, output_srt: Path, target_language: str, source_language: str, provider: str, model: str, parallel_batches: int) -> None:
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
    batch_size = 40
    batches = [(offset, texts[offset : offset + batch_size]) for offset in range(0, len(texts), batch_size)]
    translated_batches: dict[int, list[str]] = {}
    workers = max(1, min(8, parallel_batches))
    def translate_batch(offset_and_batch: tuple[int, list[str]]) -> tuple[int, list[str]]:
        offset, batch = offset_and_batch
        print(f"翻译字幕 {offset + 1}-{offset + len(batch)} / {len(texts)}")
        return offset, translate_texts_openai_compatible(batch, target_language, source_language, model)
    if workers == 1 or len(batches) <= 1:
        for batch in batches:
            offset, result = translate_batch(batch)
            translated_batches[offset] = result
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(translate_batch, batch) for batch in batches]
            for future in concurrent.futures.as_completed(futures):
                offset, result = future.result()
                translated_batches[offset] = result
    translated: list[str] = []
    for offset, _batch in batches:
        translated.extend(translated_batches[offset])
    output_items = [SubtitleItem(item.index, item.start, item.end, text) for item, text in zip(items, translated)]
    write_srt(output_items, output_srt)


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
    return re.sub(r"\s+", "", text).strip()


def paddle_ocr_predict(ocr, frame: Path):
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
    result = paddle_ocr_predict(ocr, frame)
    lines: list[tuple[float, float, str]] = []
    for points, text, confidence in iter_ocr_lines(result):
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


def create_paddle_ocr(language: str = "auto"):
    from paddleocr import PaddleOCR

    lang = ocr_lang_value(language)
    print(f"OCR 语言模型: {lang}")
    try:
        return PaddleOCR(use_angle_cls=False, lang=lang, show_log=False)
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
) -> None:
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError("未安装 PaddleOCR。可执行：pip install paddleocr paddlepaddle pillow") from exc
    if fps <= 0:
        raise ValueError("OCR fps 必须大于 0")
    output_srt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hard-subtitle-ocr-") as tmp:
        frames = extract_frames_for_ocr(video, Path(tmp), ffmpeg, fps=fps, max_frames=0, crop_bottom_percent=crop_bottom_percent)
        if not frames:
            raise RuntimeError("未能截取用于 OCR 的画面。")
        print(f"硬字幕 OCR：抽帧 {len(frames)} 张，fps={fps}")
        ocr = create_paddle_ocr(ocr_language)
        items: list[SubtitleItem] = []
        active_text = ""
        active_norm = ""
        active_start: float | None = None
        last_seen: float | None = None
        frame_step = 1.0 / fps
        for frame_index, frame in enumerate(frames):
            timestamp = frame_index * frame_step
            text = ocr_frame_text(ocr, frame, min_confidence)
            norm = normalize_ocr_text(text)
            if norm:
                if active_norm and norm == active_norm:
                    last_seen = timestamp + frame_step
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
        if active_norm and active_start is not None and last_seen is not None and last_seen - active_start >= min_duration:
            items.append(SubtitleItem(len(items) + 1, seconds_to_srt_time(active_start), seconds_to_srt_time(last_seen), active_text))
        if not items:
            raise RuntimeError("没有从硬字幕区域 OCR 出稳定字幕文本。可以改用语音识别或调高截取区域。")
        write_srt(items, output_srt)
        print(f"硬字幕 OCR 完成：{len(items)} 条 -> {output_srt}")


def detect_hard_subtitle_region(
    video: Path,
    ffmpeg: str,
    fps: float,
    max_frames: int,
    crop_bottom_percent: float,
    margin_percent: float,
    ocr_language: str = "auto",
) -> dict:
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError("未安装 PaddleOCR。可执行：pip install paddleocr paddlepaddle") from exc
    with tempfile.TemporaryDirectory(prefix="subtitle-ocr-") as tmp:
        frames = extract_frames_for_ocr(video, Path(tmp), ffmpeg, fps, max_frames, crop_bottom_percent)
        if not frames:
            raise RuntimeError("未能截取用于 OCR 的画面。")
        ocr = create_paddle_ocr(ocr_language)
        boxes: list[tuple[float, float, float, float]] = []
        for frame in frames:
            result = paddle_ocr_predict(ocr, frame)
            for points, _text, confidence in iter_ocr_lines(result):
                if confidence < 0.55:
                    continue
                bounds = points_bounds(points)
                if bounds:
                    boxes.append(bounds)
        if not boxes:
            raise RuntimeError("没有在底部区域识别到稳定字幕。可以改用手动遮盖参数。")
        min_y = min(box[1] for box in boxes)
        max_y = max(box[3] for box in boxes)
        # OCR 坐标是在下方裁剪区域内，换算为整帧百分比。
        top_percent = 100 - crop_bottom_percent + (min_y / max(1, crop_bottom_percent * 10_000)) * 100
        bottom_percent = 100 - crop_bottom_percent + (max_y / max(1, crop_bottom_percent * 10_000)) * 100
        # 上面的粗略百分比依赖截帧尺寸未知，改用 box 高度占裁剪图相对值估计不稳；
        # 因此用 PaddleOCR 图片尺寸读回精确换算。
        try:
            from PIL import Image

            with Image.open(frames[0]) as image:
                crop_h = image.height
            top_percent = 100 - crop_bottom_percent + min_y / crop_h * crop_bottom_percent
            bottom_percent = 100 - crop_bottom_percent + max_y / crop_h * crop_bottom_percent
        except Exception:
            pass
        top_percent = max(0.0, top_percent - margin_percent)
        bottom_percent = min(100.0, bottom_percent + margin_percent)
        height_percent = max(4.0, bottom_percent - top_percent)
        return {
            "cover_y_percent": round(top_percent, 2),
            "cover_height_percent": round(height_percent, 2),
            "box_count": len(boxes),
            "frames_checked": len(frames),
        }


def subtitle_style(layout: str, font_size: int, position: str) -> str:
    if position == "auto":
        position = "top" if layout == "bilingual" else "bottom"
    if position == "top":
        alignment, margin_v = 8, 45
    elif position == "above-original":
        alignment, margin_v = 2, 150
    else:
        alignment, margin_v = 2, 40
    return f"FontName=Arial,FontSize={font_size},Outline=2,Shadow=1,Alignment={alignment},MarginV={margin_v}"


def render_subtitle(
    video: Path,
    subtitle: Path,
    output: Path,
    mode: str,
    layout: str,
    position: str,
    cover: bool,
    cover_y_percent: float,
    cover_height_percent: float,
    cover_opacity: float,
    cover_color: str,
    cover_auto_detect: bool,
    cover_ocr_language: str,
    font_size: int,
    hardware_acceleration: str,
    ffmpeg: str,
    dry_run: bool,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if mode == "soft":
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
    filters: list[str] = []
    if layout not in {"replace", "bilingual"}:
        raise ValueError("layout 必须是 replace 或 bilingual")
    should_cover = cover and layout == "replace"
    if should_cover:
        if cover_auto_detect and not dry_run:
            try:
                region = detect_hard_subtitle_region(video, ffmpeg, fps=0.5, max_frames=30, crop_bottom_percent=35.0, margin_percent=1.5, ocr_language=cover_ocr_language)
                cover_y_percent = float(region["cover_y_percent"])
                cover_height_percent = float(region["cover_height_percent"])
                print(f"OCR 自动字幕区域: 起点 {cover_y_percent:.2f}%，高度 {cover_height_percent:.2f}%")
            except RuntimeError as exc:
                print(f"OCR 自动识别字幕区域失败，改用手动遮盖参数: {exc}", file=sys.stderr)
        y = f"ih*{cover_y_percent / 100:.6f}"
        h = f"ih*{cover_height_percent / 100:.6f}"
        filters.append(f"drawbox=x=0:y={y}:w=iw:h={h}:color={cover_color}@{cover_opacity:.4f}:t=fill")
    style = subtitle_style(layout, font_size, position)
    filters.append(f"subtitles='{ffmpeg_subtitle_path(subtitle)}':force_style='{style}'")
    resolved_acceleration = video_dedup.resolve_hardware_acceleration(ffmpeg, hardware_acceleration)
    encoder_args: list[str]
    if resolved_acceleration == "nvidia":
        encoder_args = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "20", "-b:v", "0"]
    elif resolved_acceleration == "amd":
        encoder_args = ["-c:v", "h264_amf", "-quality", "balanced", "-qp_i", "20", "-qp_p", "20"]
    elif resolved_acceleration == "intel":
        encoder_args = ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "20"]
    elif resolved_acceleration == "apple":
        encoder_args = ["-c:v", "h264_videotoolbox", "-b:v", "8M"]
    else:
        encoder_args = ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
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
    run(command, dry_run)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地字幕提取、翻译、替换和烧录工具")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--ffprobe", help="ffprobe 可执行文件路径")
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
    translate.add_argument("--parallel-batches", type=int, default=3, help="LLM 翻译并发批次数，默认 3")

    transcribe = sub.add_parser("transcribe", help="无字幕视频：用 faster-whisper 语音识别生成 SRT")
    transcribe.add_argument("video")
    transcribe.add_argument("output_srt")
    transcribe.add_argument("--model-size", default="medium", help="tiny/base/small/medium/large-v3 等")
    transcribe.add_argument("--language", default="auto", help="auto/zh/en/ja/ko 等")
    transcribe.add_argument("--device", default="cuda", choices=("cuda", "cpu"))

    hard_ocr = sub.add_parser("hard-ocr", help="硬字幕视频：OCR 画面字幕生成 SRT")
    hard_ocr.add_argument("video")
    hard_ocr.add_argument("output_srt")
    hard_ocr.add_argument("--fps", type=float, default=2.0)
    hard_ocr.add_argument("--crop-bottom-percent", type=float, default=35.0)
    hard_ocr.add_argument("--min-confidence", type=float, default=0.55)
    hard_ocr.add_argument("--ocr-language", default="auto", help="auto/ch/en/arabic/ar/zh")

    region = sub.add_parser("detect-region", help="硬字幕视频：用 PaddleOCR 自动估计遮盖区域")
    region.add_argument("video")
    region.add_argument("--fps", type=float, default=0.5)
    region.add_argument("--max-frames", type=int, default=30)
    region.add_argument("--crop-bottom-percent", type=float, default=35.0)
    region.add_argument("--margin-percent", type=float, default=1.5)
    region.add_argument("--ocr-language", default="auto", help="auto/ch/en/arabic/ar/zh")

    render = sub.add_parser("render", help="替换软字幕或烧录硬字幕")
    render.add_argument("video")
    render.add_argument("subtitle")
    render.add_argument("output")
    render.add_argument("--mode", choices=("soft", "burn"), default="burn")
    render.add_argument("--layout", choices=("replace", "bilingual"), default="replace", help="replace=覆盖原字幕；bilingual=不遮盖，在合适位置新增字幕")
    render.add_argument("--position", choices=("auto", "bottom", "above-original", "top"), default="auto")
    render.add_argument("--cover", action="store_true", help="烧录新字幕前遮住原字幕区域")
    render.add_argument("--cover-auto-detect", action="store_true", help="用 OCR 自动识别原字幕区域；失败时回退到手动百分比")
    render.add_argument("--cover-y-percent", type=float, default=74.0, help="遮盖区域从画面高度百分比开始")
    render.add_argument("--cover-height-percent", type=float, default=11.0, help="遮盖区域高度百分比")
    render.add_argument("--cover-opacity", type=float, default=0.72, help="遮盖蒙版透明度")
    render.add_argument("--cover-color", default="white", help="遮盖蒙版颜色，默认 white")
    render.add_argument("--cover-ocr-language", default="auto", help="自动识别遮盖区域时使用的 OCR 语言")
    render.add_argument("--font-size", type=int, default=28)
    render.add_argument("--hardware-acceleration", choices=("auto", "nvidia", "amd", "intel", "apple", "cpu"), default="auto")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
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
            translate_srt(Path(args.input_srt), Path(args.output_srt), args.target_language, args.source_language, args.provider, args.model, args.parallel_batches)
        elif args.command == "transcribe":
            transcribe_video(Path(args.video), Path(args.output_srt), args.model_size, args.language, args.device, ffmpeg)
        elif args.command == "hard-ocr":
            ocr_hard_subtitles(Path(args.video), Path(args.output_srt), ffmpeg, args.fps, args.crop_bottom_percent, args.min_confidence, ocr_language=args.ocr_language)
        elif args.command == "detect-region":
            region = detect_hard_subtitle_region(Path(args.video), ffmpeg, args.fps, args.max_frames, args.crop_bottom_percent, args.margin_percent, args.ocr_language)
            print(json.dumps(region, ensure_ascii=False, indent=2))
            print(f"建议遮盖参数：起点 {region['cover_y_percent']}%，高度 {region['cover_height_percent']}%")
        elif args.command == "render":
            render_subtitle(
                Path(args.video),
                Path(args.subtitle),
                Path(args.output),
                args.mode,
                args.layout,
                args.position,
                args.cover,
                args.cover_y_percent,
                args.cover_height_percent,
                args.cover_opacity,
                args.cover_color,
                args.cover_auto_detect,
                args.cover_ocr_language,
                args.font_size,
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
