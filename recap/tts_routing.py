from __future__ import annotations

from .narration_text import canonical_language


ENGINE_LABELS = {
    "qwen3_tts": "Qwen3 TTS",
    "fish_s2": "Fish Speech S2",
    "chatterbox_v3": "Chatterbox V3",
}

ENGINE_ALIASES = {
    "fish": "fish_s2",
    "fish_speech": "fish_s2",
    "chatterbox": "chatterbox_v3",
    "qwen": "qwen3_tts",
    "qwen3": "qwen3_tts",
}

LANGUAGE_ENGINES = {
    "Arabic": ("fish_s2", "chatterbox_v3"),
    "English": ("qwen3_tts", "fish_s2", "chatterbox_v3"),
    "Chinese": ("qwen3_tts", "fish_s2", "chatterbox_v3"),
}

DEFAULT_ENGINE = {
    "Arabic": "fish_s2",
    "English": "qwen3_tts",
    "Chinese": "qwen3_tts",
}


def explicit_engines_for_language(language: str) -> tuple[str, ...]:
    canonical = canonical_language(language)
    return LANGUAGE_ENGINES.get(canonical, ("qwen3_tts",))


def resolve_tts_engine(language: str, requested: str | None = "auto") -> str:
    canonical = canonical_language(language)
    normalized = str(requested or "auto").strip().casefold()
    normalized = ENGINE_ALIASES.get(normalized, normalized)
    if normalized == "auto":
        return DEFAULT_ENGINE.get(canonical, explicit_engines_for_language(canonical)[0])
    allowed = explicit_engines_for_language(canonical)
    if normalized not in allowed:
        allowed_labels = "、".join(ENGINE_LABELS[item] for item in allowed)
        if canonical == "Arabic" and normalized == "qwen3_tts":
            raise ValueError(
                f"Qwen3 TTS 不支持阿拉伯语；阿拉伯语只支持：{allowed_labels}"
            )
        raise ValueError(f"{canonical} 只支持以下 TTS 模型：{allowed_labels}")
    return normalized


def engines_for_language(language: str) -> list[dict[str, str]]:
    canonical = canonical_language(language)
    default_engine = DEFAULT_ENGINE.get(canonical, explicit_engines_for_language(canonical)[0])
    return [
        {"value": "auto", "label": f"自动（默认 {ENGINE_LABELS[default_engine]}）"},
        *(
            {"value": engine, "label": ENGINE_LABELS[engine]}
            for engine in explicit_engines_for_language(canonical)
        ),
    ]
