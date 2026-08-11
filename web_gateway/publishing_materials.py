from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

from .storage import JobPaths, VIDEO_SUFFIXES, safe_component


BIO_COPY_NAME = "bio.txt"
# Compatibility name for callers and older tests. New jobs only create bio.txt.
TIKTOK_COPY_NAME = BIO_COPY_NAME
PUBLISHING_METADATA_NAME = "publishing_metadata.json"
PUBLISHING_AGENT_DIRECTORY = "publishing-agent"
SUPPORTED_POSITIONS = {"top_left", "top_right", "bottom_left", "bottom_right"}
AUDIENCE_CATEGORIES = {"男频", "女频", "中性"}
SETTING_CATEGORIES = {"魔幻", "现代", "古装"}
AI_GENERATION_STATUSES = {"yes", "no", "unknown"}


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


def _language_from_text(value: str) -> str:
    lowered = value.casefold()
    if "阿拉伯" in value or "arabic" in lowered or re.search(r"[\u0600-\u06ff]", value):
        return "Arabic"
    return "English"


def _label_value(lines: list[str], labels: tuple[str, ...]) -> str:
    pattern = re.compile(
        rf"^(?:{'|'.join(re.escape(label) for label in labels)})\s*[:：]\s*(.*)$",
        flags=re.I,
    )
    for line in lines:
        match = pattern.match(line.strip())
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def _block_value(lines: list[str], labels: tuple[str, ...]) -> str:
    start = re.compile(
        rf"^(?:{'|'.join(re.escape(label) for label in labels)})\s*[:：]\s*(.*)$",
        flags=re.I,
    )
    any_label = re.compile(r"^[^#\-\s][^:：]{0,40}\s*[:：]\s*.*$")
    for index, line in enumerate(lines):
        match = start.match(line.strip())
        if not match:
            continue
        values = [match.group(1).strip()] if match.group(1).strip() else []
        for following in lines[index + 1:]:
            stripped = following.strip()
            if not stripped:
                if values:
                    break
                continue
            if any_label.match(stripped) or stripped.startswith("#"):
                break
            values.append(stripped)
        return re.sub(r"\s+", " ", " ".join(values)).strip()
    return ""


def parse_series_information(path: Path | None, fallback: str) -> dict[str, Any]:
    """Read downloader Markdown/TXT without treating its file list as synopsis."""
    fallback_title = re.sub(r"\s+", " ", str(fallback or "未命名短剧")).strip()
    result: dict[str, Any] = {
        "title": fallback_title,
        "synopsis": "",
        "language": "English",
        "platform": None,
        "platform_evidence": None,
        "is_ai_generated": "unknown",
        "source_file": path.name if path else None,
    }
    if not path or not path.is_file() or path.name.casefold() == BIO_COPY_NAME.casefold():
        return result
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    nonempty = [line.strip() for line in lines if line.strip()]
    heading = next((line.lstrip("# ").strip() for line in nonempty if line.startswith("#")), "")
    labeled_title = _label_value(lines, ("标题", "剧名", "title", "series title"))
    plain_lines = [line for line in nonempty if not line.startswith("#")]
    title = labeled_title or heading or (plain_lines[0] if plain_lines else "") or fallback_title
    synopsis = _block_value(lines, ("简介", "剧情简介", "bio", "synopsis", "description"))
    if not synopsis and not labeled_title and not heading and len(plain_lines) > 1:
        synopsis = re.sub(r"\s+", " ", " ".join(plain_lines[1:])).strip()
    language_text = _label_value(lines, ("语言", "language"))
    platform = _label_value(lines, ("归属平台", "来源平台", "平台", "source platform", "platform"))
    ai_text = _label_value(lines, ("是否AI生成", "AI生成", "AI generated", "AI-generated"))
    ai_lower = ai_text.casefold()
    if ai_lower in {"是", "yes", "true", "1", "نعم"}:
        ai_status = "yes"
    elif ai_lower in {"否", "no", "false", "0", "لا"}:
        ai_status = "no"
    else:
        ai_status = "unknown"
    result.update(
        {
            "title": re.sub(r"\s+", " ", title).strip(),
            "synopsis": synopsis,
            "language": _language_from_text(language_text or f"{title} {synopsis}"),
            "platform": platform or None,
            "platform_evidence": f"{path.name} 明确写明归属平台：{platform}" if platform else None,
            "is_ai_generated": ai_status,
        }
    )
    return result


def series_information(path: Path | None, fallback: str) -> tuple[str, str]:
    information = parse_series_information(path, fallback)
    return str(information["title"]), str(information["synopsis"])


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
    schema_version = int(payload.get("schema_version") or 1)
    ai_status = str(payload.get("is_ai_generated") or "unknown").strip().casefold()
    if ai_status not in AI_GENERATION_STATUSES:
        raise ValueError("is_ai_generated 必须是 yes、no 或 unknown")
    title_zh = re.sub(r"\s+", " ", str(payload.get("title_zh") or "")).strip()
    bio_zh = re.sub(r"\s+", " ", str(payload.get("bio_zh") or "")).strip()
    classification = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}
    audience = str(classification.get("audience") or "").strip()
    setting = str(classification.get("setting") or "").strip()
    confidence = float(classification.get("confidence", 0) or 0)
    rationale = re.sub(r"\s+", " ", str(classification.get("rationale") or "")).strip()
    if schema_version >= 2:
        if not 2 <= len(title_zh) <= 150 or not 8 <= len(bio_zh) <= 500:
            raise ValueError("schema v2 必须包含有效的中文标题和中文 Bio")
        if audience not in AUDIENCE_CATEGORIES or setting not in SETTING_CATEGORIES:
            raise ValueError("分类必须是男频/女频/中性与魔幻/现代/古装")
        if not 0 <= confidence <= 1 or len(rationale) < 4:
            raise ValueError("分类必须包含 0-1 置信度和简短依据")
    language = str(payload.get("language") or "English").strip().title()
    if language not in {"English", "Arabic"}:
        raise ValueError("发布文案语言目前仅支持 English 或 Arabic")
    if language == "Arabic" and not re.search(r"[\u0600-\u06ff]", f"{title} {bio}"):
        raise ValueError("阿拉伯语元数据必须生成阿拉伯语标题和 Bio")
    if schema_version >= 2 and not re.search(r"[\u4e00-\u9fff]", f"{title_zh} {bio_zh}"):
        raise ValueError("中文平台标题和 Bio 必须包含中文")
    return {
        "schema_version": max(1, schema_version),
        "task_type": "publishing_materials",
        "language": language,
        "platform": platform or None,
        "platform_evidence": evidence or None,
        "is_ai_generated": ai_status,
        "title": title,
        "bio": bio,
        "hashtags": tags,
        "title_zh": title_zh or None,
        "bio_zh": bio_zh or None,
        "classification": {
            "audience": audience or None,
            "setting": setting or None,
            "confidence": confidence,
            "rationale": rationale or None,
        },
        "cover": {
            "episode_number_position": position,
            "number_color": "#FFD700",
            "outline_color": "#372300",
        },
        "quality_score": score,
        "quality_notes": notes,
    }


def _ai_status_line(language: str, status: str) -> str:
    if language == "Arabic":
        return {
            "yes": "مولّد بالذكاء الاصطناعي: نعم",
            "no": "مولّد بالذكاء الاصطناعي: لا",
            "unknown": "مولّد بالذكاء الاصطناعي: غير معروف",
        }[status]
    return {
        "yes": "AI-generated: Yes",
        "no": "AI-generated: No",
        "unknown": "AI-generated: Unknown",
    }[status]


def write_bio_copy(path: Path, plan: dict[str, Any]) -> Path:
    validated = validate_publishing_plan(plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{_ai_status_line(validated['language'], validated['is_ai_generated'])}\n"
        f"{validated['title']}\n{validated['bio']}\n{' '.join(validated['hashtags'])}",
        encoding="utf-8",
    )
    return path


def write_tiktok_copy(path: Path, plan: dict[str, Any]) -> Path:
    return write_bio_copy(path, plan)


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
    cover_source = first_file(paths.assets / "cover", {".png", ".jpg", ".jpeg", ".webp", ".bmp"})
    target_root = paths.videos / "recap" if recap else paths.videos
    target_root.mkdir(parents=True, exist_ok=True)
    copy_file = write_bio_copy(target_root / BIO_COPY_NAME, validated)
    metadata_file = target_root / PUBLISHING_METADATA_NAME
    metadata_file.write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
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
        "metadata_file": str(metadata_file),
        "cover_files": [str(path) for path in covers],
        "cover_source": str(cover_source) if cover_source else None,
        "plan": validated,
    }
