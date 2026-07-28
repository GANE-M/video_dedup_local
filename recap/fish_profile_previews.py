from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
from typing import Any

from .fish_voice_candidates import _audio_metrics


def pad_to_minimum(path: Path, seconds: float = 10.0) -> None:
    import numpy as np
    import soundfile as sf

    audio, sample_rate = sf.read(str(path), always_2d=False)
    minimum = int(float(sample_rate) * seconds)
    if len(audio) >= minimum:
        return
    padding = ((0, minimum - len(audio)),) if audio.ndim == 1 else ((0, minimum - len(audio)), (0, 0))
    sf.write(str(path), np.pad(audio, padding), sample_rate)


def generate_previews(payload: dict[str, Any]) -> dict[str, Any]:
    import soundfile as sf
    import torch
    from fish_speech.models.text2semantic.inference import (
        decode_to_audio,
        encode_audio,
        generate_long,
        init_model,
        load_codec_model,
    )

    checkpoint = Path(payload["checkpoint"]).resolve()
    manifest = Path(payload["manifest"]).resolve()
    data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    profiles = list(data.get("profiles") or [])
    asset_root = manifest.parent
    device, precision = "cuda", torch.float16
    model, decode_one_token = init_model(
        checkpoint, device, precision, compile=False,
        max_length=int(payload.get("max_seq_len", 4096)), bnb4=True,
    )
    with torch.device(device):
        model.setup_caches(max_batch_size=1, max_seq_len=model.config.max_seq_len, dtype=next(model.parameters()).dtype)
    codec = load_codec_model(checkpoint / "codec.pth", device, precision)
    try:
        for index, profile in enumerate(profiles, 1):
            reference_value = Path(profile["reference_audio"])
            output_value = Path(profile["preview_audio"])
            reference = (reference_value if reference_value.is_absolute() else asset_root / reference_value).resolve()
            output = (output_value if output_value.is_absolute() else asset_root / output_value).resolve()
            if not output.is_file():
                prompt_tokens = encode_audio(reference, codec, device).cpu()
                torch.manual_seed(int(payload.get("seed", 8800)) + index)
                torch.cuda.manual_seed(int(payload.get("seed", 8800)) + index)
                codes = []
                for response in generate_long(
                    model=model, device=device, decode_one_token=decode_one_token,
                    text=profile["preview_text"], num_samples=1, max_new_tokens=0,
                    top_p=float(profile["generation_parameters"].get("top_p", 0.9)),
                    top_k=int(profile["generation_parameters"].get("top_k", 30)),
                    temperature=float(profile["generation_parameters"].get("temperature", 0.88)),
                    compile=False, iterative_prompt=True, chunk_length=300,
                    prompt_text=[profile["reference_text"]], prompt_tokens=[prompt_tokens],
                ):
                    if response.action == "sample":
                        codes.append(response.codes)
                    elif response.action == "next":
                        break
                if not codes:
                    raise RuntimeError(f"Fish S2 returned no codes for {profile['voice_id']}")
                output.parent.mkdir(parents=True, exist_ok=True)
                audio = decode_to_audio(torch.cat(codes, dim=1).to(device), codec)
                sf.write(str(output), audio.cpu().float().numpy(), codec.sample_rate)
            pad_to_minimum(output)
            profile["preview_metrics"] = _audio_metrics(output)
            print(f"{index}/{len(profiles)} {profile['voice_id']}", flush=True)
    finally:
        del codec, model
        gc.collect()
        torch.cuda.empty_cache()
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "manifest": str(manifest), "profiles": len(profiles)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    args = parser.parse_args()
    print(json.dumps(generate_previews(json.loads(args.payload.read_text(encoding="utf-8-sig"))), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
