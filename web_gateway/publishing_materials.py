from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

from .storage import JobPaths, VIDEO_SUFFIXES, safe_component


TIKTOK_COPY_NAME = "TikTok发布信息.txt"
PUBLISHING_AGENT_DIRECTORY = "publishing-agent"
SUPPORTED_POSITIONS = {"top_left", "top_right", "bottom_left", "bottom_right"}


def natural_key(path: Path) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", path.name)
    )


def first_file(directory: Path, suffixes: Iterable[str]) -> Path | None:
    allowed = {str(suffix).casefold() for suffix in suffixes}
    if not directory.is_dir():
        return None
    files = sorted(
        (item for item in directory.iterdir() if item.is_file() and item.suffix.casefold() in allowed),
        key=natural_key,
    )
    return files[0] if files else None


def series_information(path: Path | None, fallback: str) -> tuple[str, str]:
    title = re.sub(r"\s+", " ", str(fallback or "未命名短剧")).strip()
    synopsis = ""
    if path and path.is_file() and path.name.casefold() != TIKTOK_COPY_NAME.casefold():
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            title = re.sub(r"^(?:标题|剧名|title)\s*[:：-]?\s*", "", lines[0], flags=re.I).strip() or title
        if len(lines) > 1:
            synopsis = " ".join(line for line in lines[1:] if not line.startswith("#"))
            synopsis = re.sub(
                r"^(?:简介|剧情简介|bio|synopsis|description)\s*[:：-]?\s*",
                "",
                synopsis,
                flags=re.I,
            ).strip()
    return title, re.sub(r"\s+", " ", synopsis).strip()


def normalize_hashtag(value: object) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    if not text:
        return ""
    text = "#" + text.lstrip("#")
    if not re.fullmatch(r"#[\w\u0600-\u06ff\u4e00-\u9fff.-]{1,48}", text, flags=re.UNICODE):
        raise ValueError(f"无效 hashtag: {value!r}")
    return text


def validate_publishing_plan(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("task_type") or "") != "publishing_materials":
        raise ValueError("发布物料响应 task_type 必须是 publishing_materials")
    title = re.sub(r"\s+", " ", str(payload.get("title") or "")).strip()
    bio = re.sub(r"\s+", " ", str(payload.get("bio") or "")).strip()
    if not 2 <= len(title) <= 150:
        raise ValueError("发布标题长度必须在 2-150 个字符之间")
    if not 8 <= len(bio) <= 500:
        raise ValueError("发布 Bio 长度必须在 8-500 个字符之间")
    raw_tags = payload.get("hashtags")
    if not isinstance(raw_tags, list):
        raise ValueError("hashtags 必须是数组")
    tags: list[str] = []
    for value in raw_tags:
        tag = normalize_hashtag(value)
        if tag and tag.casefold() not in {item.casefold() for item in tags}:
            tags.append(tag)
    if not 5 <= len(tags) <= 7:
        raise ValueError("hashtag 必须为 5-7 个且不得重复")

    platform = re.sub(r"[^A-Za-z0-9_.-]", "", str(payload.get("platform") or "")).strip()
    evidence = re.sub(r"\s+", " ", str(payload.get("platform_evidence") or "")).strip()
    if platform:
        platform_tag = normalize_hashtag(platform)
        if not evidence:
            raise ValueError("识别平台后必须提供 platform_evidence；不得猜测平台")
        if tags[0].casefold() != platform_tag.casefold() or tags[1].casefold() != "#fyp":
            raise ValueError("已识别平台时 hashtag 顺序必须是平台标签、#fyp、相关标签")
    elif tags[0].casefold() != "#fyp":
        raise ValueError("无法确认平台时第一个 hashtag 必须是 #fyp")

    cover = payload.get("cover") if isinstance(payload.get("cover"), dict) else {}
    position = str(cover.get("episode_number_position") or "bottom_right").casefold()
    if position not in SUPPORTED_POSITIONS:
        raise ValueError(f"不支持的封面集数位置: {position}")
    score = float(payload.get("quality_score", 0))
    if score < 8.5 or score > 10:
        raise ValueError("发布物料 Agent 自检分必须达到 8.5/10")
    notes = re.sub(r"\s+", " ", str(payload.get("quality_notes") or "")).strip()
    if len(notes) < 8:
        raise ValueError("发布物料响应必须包含简短的自检说明")
    return {
        "schema_version": 1,
        "task_type": "publishing_materials",
        "language": re.sub(r"\s+", " ", str(payload.get("language") or "English")).strip(),
        "platform": platform or None,
        "platform_evidence": evidence or None,
        "title": title,
        "bio": bio,
        "hashtags": tags,
        "cover": {
            "episode_number_position": position,
            "number_color": "#FFD700",
            "outline_color": "#372300",
        },
        "quality_score": score,
        "quality_notes": notes,
    }


def write_tiktok_copy(path: Path, plan: dict[str, Any]) -> Path:
    validated = validate_publishing_plan(plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{validated['title']}\n{validated['bio']}\n{' '.join(validated['hashtags'])}",
        encoding="utf-8",
    )
    return path


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def render_episode_cover(source: Path, destination: Path, episode: int, position: str) -> Path:
    with Image.open(source) as opened:
        image = opened.convert("RGBA")
    width, height = image.size
    font_size = max(42, round(min(width, height) * 0.23))
    outline = max(3, round(font_size * 0.055))
    margin = max(18, round(min(width, height) * 0.055))
    text = str(episode)
    selected_font = font(font_size)
    draw = ImageDraw.Draw(image)
    box = draw.textbbox((0, 0), text, font=selected_font, stroke_width=outline)
    text_width, text_height = box[2] - box[0], box[3] - box[1]
    right = position.endswith("right")
    bottom = position.startswith("bottom")
    x = width - margin - text_width if right else margin
    y = height - margin - text_height if bottom else margin
    shadow = max(2, round(outline * 0.8))
    draw.text(
        (x + shadow, y + shadow), text, font=selected_font,
        fill=(0, 0, 0, 180), stroke_width=outline + 1, stroke_fill=(0, 0, 0, 190),
    )
    draw.text(
        (x, y), text, font=selected_font,
        fill=(255, 215, 0, 255), stroke_width=outline, stroke_fill=(55, 35, 0, 255),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(destination, format="PNG", optimize=True)
    return destination


def episode_videos(paths: JobPaths) -> list[Path]:
    outputs = sorted(
        (item for item in paths.videos.iterdir() if item.is_file() and item.suffix.casefold() in VIDEO_SUFFIXES),
        key=natural_key,
    )
    if outputs:
        return outputs
    return sorted(
        (item for item in paths.input.iterdir() if item.is_file() and item.suffix.casefold() in VIDEO_SUFFIXES),
        key=natural_key,
    )


def build_publishing_materials(
    paths: JobPaths,
    plan: dict[str, Any],
    *,
    recap: bool = False,
) -> dict[str, Any]:
    """Render an accepted Agent plan into deterministic project artifacts."""
    validated = validate_publishing_plan(plan)
    cover_source = first_file(paths.assets / "cover", {".png"})
    target_root = paths.videos / "recap" if recap else paths.videos
    target_root.mkdir(parents=True, exist_ok=True)
    copy_file = write_tiktok_copy(target_root / TIKTOK_COPY_NAME, validated)
    covers: list[Path] = []
    if cover_source:
        if recap:
            target = target_root / safe_component(cover_source.name, fallback="cover.png")
            shutil.copy2(cover_source, target)
            covers.append(target)
        else:
            position = validated["cover"]["episode_number_position"]
            for index, video in enumerate(episode_videos(paths), start=1):
                target = target_root / f"{safe_component(video.stem, fallback=f'episode_{index:03d}')}_cover.png"
                covers.append(render_episode_cover(cover_source, target, index, position))
    return {
        "copy_file": str(copy_file),
        "cover_files": [str(path) for path in covers],
        "cover_source": str(cover_source) if cover_source else None,
        "plan": validated,
    }
