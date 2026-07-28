from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .narration_text import canonical_language


@dataclass(frozen=True)
class NarrationPreset:
    key: str
    label: str
    target_ratio: float
    narration_share: float
    speech_occupancy: float
    minimum_occupancy: float
    maximum_occupancy: float
    narration_seconds: tuple[float, float]
    original_seconds: tuple[float, float]
    lead_in_seconds: float
    tail_seconds: float
    speed_adjustment_limit: float
    fit_policy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "target_ratio": self.target_ratio,
            "narration_share": self.narration_share,
            "speech_occupancy": self.speech_occupancy,
            "minimum_occupancy": self.minimum_occupancy,
            "maximum_occupancy": self.maximum_occupancy,
            "narration_seconds": list(self.narration_seconds),
            "original_seconds": list(self.original_seconds),
            "lead_in_seconds": self.lead_in_seconds,
            "tail_seconds": self.tail_seconds,
            "speed_adjustment_limit": self.speed_adjustment_limit,
            "fit_policy": self.fit_policy,
        }


PRESETS: dict[str, NarrationPreset] = {
    "fast": NarrationPreset(
        "fast", "快节奏", 0.38, 0.35, 0.92, 0.80, 1.06,
        (6.0, 10.0), (8.0, 25.0), 0.25, 0.60, 0.08, "trim_to_voice",
    ),
    "standard": NarrationPreset(
        "standard", "标准解说", 0.50, 0.25, 0.90, 0.78, 1.06,
        (8.0, 14.0), (20.0, 45.0), 0.30, 0.80, 0.08, "trim_to_voice",
    ),
    "immersive": NarrationPreset(
        "immersive", "沉浸剧情", 0.65, 0.15, 0.85, 0.72, 1.08,
        (8.0, 15.0), (30.0, 60.0), 0.35, 1.00, 0.08, "trim_to_voice",
    ),
    # Projects created before pacing budgets existed retain their exact render
    # behaviour. New projects always write an explicit non-legacy preset.
    "legacy": NarrationPreset(
        "legacy", "旧项目兼容", 0.50, 0.25, 0.90, 0.0, math.inf,
        (0.0, math.inf), (0.0, math.inf), 0.0, 0.0, 0.0, "preserve_window",
    ),
}

PRESET_ALIASES = {
    "quick": "fast",
    "rapid": "fast",
    "快节奏": "fast",
    "default": "standard",
    "medium": "standard",
    "标准": "standard",
    "标准解说": "standard",
    "story": "immersive",
    "沉浸": "immersive",
    "沉浸剧情": "immersive",
}

DEFAULT_SPEECH_RATES = {
    "Arabic": {"unit": "wps", "value": 2.0, "minimum": 1.8, "maximum": 2.2},
    "English": {"unit": "wps", "value": 2.6, "minimum": 2.3, "maximum": 2.9},
    "Chinese": {"unit": "cps", "value": 4.2, "minimum": 3.8, "maximum": 4.6},
}


def normalize_preset(value: str | None, *, default: str = "standard") -> str:
    key = str(value or default).strip().casefold()
    key = PRESET_ALIASES.get(key, key)
    if key not in PRESETS:
        raise ValueError(f"未知解说预设: {value!r}")
    return key


def get_preset(value: str | None, *, default: str = "standard") -> NarrationPreset:
    return PRESETS[normalize_preset(value, default=default)]


def speech_rate(language: str, override: float | None = None) -> dict[str, float | str]:
    canonical = canonical_language(language)
    profile = dict(DEFAULT_SPEECH_RATES.get(canonical, DEFAULT_SPEECH_RATES["English"]))
    if override is not None:
        value = float(override)
        if value <= 0:
            raise ValueError("实测口播语速必须大于 0")
        profile["value"] = value
    return profile


def count_speech_units(text: str, language: str) -> int:
    value = str(text or "").strip()
    canonical = canonical_language(language)
    if canonical == "Chinese":
        chinese = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", value)
        latin_words = re.findall(r"[A-Za-z0-9]+", value)
        return len(chinese) + len(latin_words)
    if canonical == "Arabic":
        # Keep Arabic combining marks inside their word instead of treating a
        # vocalised form such as "غيّر" as two units. Arabic punctuation is
        # excluded by requiring at least one letter in each token.
        arabic = [
            token
            for token in re.findall(r"[\u0600-\u06ff]+", value)
            if re.search(r"[\u0621-\u063a\u0641-\u064a\u066e-\u06d3\u06fa-\u06ff]", token)
        ]
        latin_words = re.findall(r"[A-Za-z0-9]+", value)
        return len(arabic) + len(latin_words)
    return len(re.findall(r"[^\W_]+(?:['’\-][^\W_]+)*", value, flags=re.UNICODE))


def build_duration_budget(
    *,
    preset_name: str,
    target_duration_seconds: float,
    target_language: str,
    narration_speed: float = 1.0,
    speech_rate_override: float | None = None,
) -> dict[str, Any]:
    preset = get_preset(preset_name)
    rate = speech_rate(target_language, speech_rate_override)
    target = max(0.0, float(target_duration_seconds))
    narration_seconds = target * preset.narration_share
    original_seconds = max(0.0, target - narration_seconds)
    effective_rate = float(rate["value"]) * max(0.5, min(2.0, float(narration_speed)))
    target_units = narration_seconds * effective_rate * preset.speech_occupancy
    return {
        "preset": preset.to_dict(),
        "target_duration_seconds": round(target, 3),
        "narration_duration_seconds": round(narration_seconds, 3),
        "original_duration_seconds": round(original_seconds, 3),
        "speech_rate": {
            **rate,
            "effective_value": round(effective_rate, 4),
            "narration_speed": float(narration_speed),
        },
        "target_narration_units": round(target_units),
        "allowed_narration_units": [
            math.floor(narration_seconds * effective_rate * preset.minimum_occupancy),
            math.ceil(narration_seconds * effective_rate * min(1.0, preset.maximum_occupancy)),
        ],
        "formula": (
            "target_units = narration_duration_seconds * speech_rate.effective_value "
            "* preset.speech_occupancy"
        ),
    }


def segment_pacing(
    *,
    text: str,
    duration_seconds: float,
    target_language: str,
    preset_name: str,
    narration_speed: float = 1.0,
    speech_rate_override: float | None = None,
) -> dict[str, Any]:
    preset = get_preset(preset_name)
    rate = speech_rate(target_language, speech_rate_override)
    units = count_speech_units(text, target_language)
    effective_rate = float(rate["value"]) * max(0.5, min(2.0, float(narration_speed)))
    speech_seconds = units / effective_rate if effective_rate else 0.0
    duration = max(0.001, float(duration_seconds))
    usable = max(0.001, duration - preset.lead_in_seconds - preset.tail_seconds)
    occupancy = speech_seconds / usable
    target_units = usable * effective_rate * preset.speech_occupancy
    return {
        "units": units,
        "unit": rate["unit"],
        "speech_rate": round(effective_rate, 4),
        "estimated_speech_seconds": round(speech_seconds, 3),
        "available_speech_seconds": round(usable, 3),
        "estimated_occupancy": round(occupancy, 4),
        "target_units": round(target_units),
        "allowed_units": [
            math.floor(usable * effective_rate * preset.minimum_occupancy),
            math.ceil(usable * effective_rate * preset.maximum_occupancy),
        ],
        "status": (
            "short"
            if occupancy < preset.minimum_occupancy
            else "long"
            if occupancy > preset.maximum_occupancy
            else "ok"
        ),
    }


def fitted_narration_seconds(
    *,
    planned_seconds: float,
    voice_seconds: float,
    lead_in_seconds: float,
    tail_seconds: float,
    fit_policy: str,
    hold_visual: bool = False,
) -> float:
    planned = max(0.001, float(planned_seconds))
    voice_end = max(0.001, float(lead_in_seconds) + float(voice_seconds))
    fitted = voice_end + max(0.0, float(tail_seconds))
    policy = str(fit_policy or "").strip().casefold()
    if hold_visual or policy == "preserve_window":
        return max(planned, voice_end)
    if policy == "trim_to_voice":
        return fitted
    raise ValueError(f"unsupported narration_fit_policy={fit_policy!r}")
