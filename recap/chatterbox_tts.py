from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
from typing import Any


LANGUAGE_IDS = {
    "Arabic": "ar",
    "English": "en",
    "Chinese": "zh",
}


def generate_batch(payload: dict[str, Any]) -> dict[str, Any]:
    import soundfile
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    profile = payload["profile"]
    reference_audio = Path(profile["reference_audio"]).resolve()
    if not reference_audio.is_file():
        raise FileNotFoundError(f"voice reference is missing: {reference_audio}")
    model = ChatterboxMultilingualTTS.from_pretrained(device="cuda", t3_model="v3")
    generated = []
    try:
        for item in payload.get("items", []):
            output = Path(item["output"]).resolve()
            if output.is_file():
                generated.append({"segment_id": item["segment_id"], "path": str(output), "cache_hit": True})
                continue
            language = str(item.get("language") or "English")
            language_id = LANGUAGE_IDS.get(language)
            if not language_id:
                raise ValueError(f"Chatterbox V3 暂不支持目标语言: {language}")
            output.parent.mkdir(parents=True, exist_ok=True)
            with torch.inference_mode():
                wav = model.generate(
                    str(item["text"]),
                    language_id=language_id,
                    audio_prompt_path=str(reference_audio),
                )
            soundfile.write(str(output), wav.detach().cpu().squeeze().numpy(), model.sr)
            generated.append({"segment_id": item["segment_id"], "path": str(output), "cache_hit": False})
    finally:
        del model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    return {"status": "ok", "engine": "chatterbox_v3", "generated": generated}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text(encoding="utf-8-sig"))
    print(json.dumps(generate_batch(payload), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
