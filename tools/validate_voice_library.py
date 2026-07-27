from __future__ import annotations

import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "recap" / "voices" / "library.json"
sys.path.insert(0, str(ROOT))

from tts_lab.validate_asr import WhisperModel
from recap.fish_voice_candidates import _audio_metrics


def normalized(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.findall(r"[\w]+", text.casefold(), flags=re.UNICODE))


def validate_candidates(manifest: Path, model: WhisperModel) -> Path:
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    items = payload.get("candidates") or payload.get("profiles") or []
    for item in items:
        audio = Path(item.get("preview_audio") or item["reference_audio"])
        if not audio.is_absolute():
            audio = manifest.parent / audio
        try:
            if item.get("preview_audio"):
                item["preview_metrics"] = _audio_metrics(audio)
            else:
                item["metrics"] = _audio_metrics(audio)
        except ModuleNotFoundError:
            # The lean faster-whisper environment intentionally omits SoundFile.
            # Generation already wrote the technical metrics; ASR validation can
            # proceed without pulling a second audio stack into that environment.
            pass
        languages = item.get("languages") or [item.get("language")]
        language = "ar" if "Arabic" in languages else "en"
        segments, info = model.transcribe(str(audio), language=language, beam_size=5, vad_filter=True)
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
        expected = item.get("preview_text") if item.get("preview_audio") else item["reference_text"]
        similarity = round(difflib.SequenceMatcher(
            None, normalized(expected), normalized(transcript), autojunk=False,
        ).ratio(), 3)
        item["asr_validation"] = {
            "transcript": transcript,
            "similarity": similarity,
            "language_probability": round(info.language_probability, 3),
            "passed": similarity >= 0.9,
        }
        technical = float((item.get("preview_metrics") or item.get("metrics") or {}).get("technical_score", 0.0))
        item["quality_score"] = round(min(technical, similarity * 10.0), 2)
        item["review_status"] = "approved" if item["quality_score"] >= 8.5 else "rejected"
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, action="append", default=[])
    args = parser.parse_args()
    model = WhisperModel("medium", device="cuda", compute_type="float16")
    if args.manifest:
        for manifest in args.manifest:
            print(validate_candidates(manifest.resolve(), model))
        return 0

    payload = json.loads(LIBRARY.read_text(encoding="utf-8-sig"))
    results = []
    for profile in payload["voices"]:
        preview = LIBRARY.parent / profile["preview_audio"]
        language = "ar" if "Arabic" in profile["languages"] else "en"
        segments, info = model.transcribe(str(preview), language=language, beam_size=5, vad_filter=True)
        transcript = " ".join(item.text.strip() for item in segments).strip()
        results.append({
            "voice_id": profile["voice_id"],
            "language": language,
            "transcript": transcript,
            "expected": profile["preview_text"],
            "similarity": round(
                difflib.SequenceMatcher(
                    None,
                    normalized(profile["preview_text"]),
                    normalized(transcript),
                    autojunk=False,
                ).ratio(),
                3,
            ),
            "language_probability": round(info.language_probability, 3),
        })
    output = LIBRARY.parent / "validation.json"
    output.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
