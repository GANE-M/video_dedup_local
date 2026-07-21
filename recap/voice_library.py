from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import VoiceProfile, stable_hash


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_LIBRARY_PATH = PACKAGE_ROOT / "voices" / "library.json"


class VoiceLibrary:
    def __init__(self, path: Path = DEFAULT_LIBRARY_PATH) -> None:
        self.path = Path(path).resolve()
        payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        self.schema_version = int(payload.get("schema_version", 0))
        if self.schema_version != 1:
            raise ValueError(f"unsupported voice library schema: {self.schema_version}")
        self._profiles = {item.voice_id: item for item in map(VoiceProfile.from_dict, payload.get("voices", []))}
        if len(self._profiles) != len(payload.get("voices", [])):
            raise ValueError("voice_id values must be unique")
        for profile in self._profiles.values():
            if not profile.voice_id or not profile.reference_audio or not profile.reference_text:
                raise ValueError(f"voice profile is incomplete: {profile.voice_id or '<missing>'}")

    def resolve_asset(self, value: str) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.path.parent / path).resolve()

    def list(self) -> list[VoiceProfile]:
        return list(self._profiles.values())

    def get(self, voice_id: str) -> VoiceProfile:
        try:
            return self._profiles[voice_id]
        except KeyError as exc:
            raise KeyError(f"unknown voice_id {voice_id!r}; available={sorted(self._profiles)}") from exc

    def validate_assets(self, voice_id: str, *, require_preview: bool = False) -> list[str]:
        profile = self.get(voice_id)
        errors = []
        if not self.resolve_asset(profile.reference_audio).is_file():
            errors.append(f"missing reference_audio: {self.resolve_asset(profile.reference_audio)}")
        preview = self.resolve_asset(profile.preview_audio)
        if require_preview and profile.preview_audio and not preview.is_file():
            errors.append(f"missing preview_audio: {preview}")
        return errors


def voice_cache_key(profile: VoiceProfile, text: str, language: str, speed: float, parameters: dict[str, Any] | None = None) -> str:
    return stable_hash({
        "voice_id": profile.voice_id,
        "text": text,
        "language": language,
        "speed": round(float(speed), 6),
        "model_version": profile.model_version,
        "engine": profile.engine,
        "generation_parameters": parameters if parameters is not None else profile.generation_parameters,
    })


def voice_cache_path(cache_root: Path, project_id: str, profile: VoiceProfile, cache_key: str) -> Path:
    return Path(cache_root).resolve() / project_id / profile.voice_id / f"{cache_key}.wav"
