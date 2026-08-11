from __future__ import annotations

import re
import unicodedata


ARABIC_LANGUAGE_NAMES = {"ar", "arabic", "العربية", "阿拉伯语", "阿语"}
_BIDI_CONTROLS = dict.fromkeys(
    map(
        ord,
        "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
        "\u2066\u2067\u2068\u2069\ufeff",
    ),
    None,
)
_ARABIC_LETTER = re.compile(r"[\u0621-\u063a\u0641-\u064a]")


def canonical_language(value: str) -> str:
    language = str(value or "").strip().casefold()
    if language in ARABIC_LANGUAGE_NAMES:
        return "Arabic"
    if language in {"en", "english", "英语", "英文"}:
        return "English"
    if language in {"zh", "chinese", "中文", "汉语", "普通话"}:
        return "Chinese"
    return str(value or "English").strip() or "English"


def is_arabic_language(value: str) -> bool:
    return canonical_language(value) == "Arabic"


def normalize_narration_text(text: str, language: str) -> str:
    """Normalize TTS input while preserving Unicode logical reading order."""
    value = unicodedata.normalize("NFKC", str(text or "")).translate(_BIDI_CONTROLS)
    value = value.replace("\ufffd", "")
    value = re.sub(r"[\t\v\f\r ]+", " ", value)
    value = re.sub(r"\s*\n+\s*", "، " if is_arabic_language(language) else " ", value)
    value = re.sub(r" {2,}", " ", value).strip()
    if not value:
        raise ValueError("解说文本为空")
    if is_arabic_language(language):
        value = value.replace("ـ", "")
        value = re.sub(r"\s*([،؛؟])\s*", r"\1 ", value)
        value = re.sub(r"\s+([.!])", r"\1", value)
        value = re.sub(r"([،؛؟.!]){2,}", r"\1", value).strip()
        letters = re.findall(r"[^\W\d_]", value, flags=re.UNICODE)
        if letters and not _ARABIC_LETTER.search(value):
            raise ValueError("目标语言为阿拉伯语，但解说文本中没有检测到阿拉伯字母")
    return value


def split_caption_sentences(text: str, language: str) -> list[str]:
    normalized = normalize_narration_text(text, language)
    punctuation = r"(?<=[.!?。！？])\s+"
    if is_arabic_language(language):
        punctuation = r"(?<=[.!؟؛])\s+"
    return [item.strip() for item in re.split(punctuation, normalized) if item.strip()] or [normalized]


def wrap_caption(text: str, width: int) -> str:
    """Wrap by words so Arabic grapheme order is never reversed or sliced."""
    words = text.split()
    if not words:
        return text
    lines: list[str] = []
    current: list[str] = []
    current_length = 0
    for word in words:
        added = len(word) + (1 if current else 0)
        if current and current_length + added > width:
            lines.append(" ".join(current))
            current = [word]
            current_length = len(word)
        else:
            current.append(word)
            current_length += added
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)
