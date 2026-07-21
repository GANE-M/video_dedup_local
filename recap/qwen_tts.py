from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
from typing import Any


def _load_dependencies():
    try:
        import soundfile as sf
        import torch
        from qwen_tts import Qwen3TTSModel
    except ImportError as exc:
        raise RuntimeError("Qwen TTS dependencies are missing; use the configured Qwen Python environment") from exc
    return sf, torch, Qwen3TTSModel


def load_model(model_name: str):
    _sf, torch, model_type = _load_dependencies()
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    return model_type.from_pretrained(model_name, device_map=device, dtype=dtype, attn_implementation="sdpa")


def generate_batch(payload: dict[str, Any]) -> dict[str, Any]:
    sf, torch, _model_type = _load_dependencies()
    profile = payload["profile"]
    reference_audio = Path(profile["reference_audio"]).resolve()
    if not reference_audio.is_file():
        raise FileNotFoundError(f"voice reference is missing: {reference_audio}")
    model = load_model(str(profile["model_version"]))
    prompt = model.create_voice_clone_prompt(
        ref_audio=str(reference_audio), ref_text=str(profile["reference_text"]), x_vector_only_mode=False,
    )
    generated = []
    try:
        for item in payload.get("items", []):
            output = Path(item["output"]).resolve()
            if output.is_file():
                generated.append({"segment_id": item["segment_id"], "path": str(output), "cache_hit": True})
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            parameters = dict(profile.get("generation_parameters") or {})
            wavs, sample_rate = model.generate_voice_clone(
                text=str(item["text"]), language=str(item["language"]), voice_clone_prompt=prompt,
                non_streaming_mode=True, **parameters,
            )
            sf.write(output, wavs[0], sample_rate)
            generated.append({"segment_id": item["segment_id"], "path": str(output), "cache_hit": False})
    finally:
        del model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    return {"status": "ok", "generated": generated}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text(encoding="utf-8-sig"))
    print(json.dumps(generate_batch(payload), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
