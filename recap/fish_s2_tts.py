from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
from typing import Any


def generate_batch(payload: dict[str, Any]) -> dict[str, Any]:
    import soundfile
    import torch
    from fish_speech.models.text2semantic.inference import (
        decode_to_audio,
        encode_audio,
        generate_long,
        init_model,
        load_codec_model,
    )

    profile = payload["profile"]
    checkpoint = Path(payload["checkpoint"]).resolve()
    reference_audio = Path(profile["reference_audio"]).resolve()
    reference_text = str(profile["reference_text"]).strip()
    if not reference_audio.is_file():
        raise FileNotFoundError(f"voice reference is missing: {reference_audio}")
    if not (checkpoint / "model.pth").is_file() or not (checkpoint / "codec.pth").is_file():
        raise FileNotFoundError(f"Fish S2 checkpoint is incomplete: {checkpoint}")

    device = "cuda"
    precision = torch.float16
    model, decode_one_token = init_model(
        checkpoint,
        device,
        precision,
        compile=False,
        max_length=int(payload.get("max_seq_len", 4096)),
        bnb4=True,
    )
    with torch.device(device):
        model.setup_caches(
            max_batch_size=1,
            max_seq_len=model.config.max_seq_len,
            dtype=next(model.parameters()).dtype,
        )
    codec = load_codec_model(checkpoint / "codec.pth", device, precision)
    prompt_tokens = encode_audio(reference_audio, codec, device).cpu()
    generated = []
    try:
        for index, item in enumerate(payload.get("items", [])):
            output = Path(item["output"]).resolve()
            if output.is_file():
                generated.append({"segment_id": item["segment_id"], "path": str(output), "cache_hit": True})
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            torch.manual_seed(int(payload.get("seed", 2026)) + index)
            torch.cuda.manual_seed(int(payload.get("seed", 2026)) + index)
            codes = []
            for response in generate_long(
                model=model,
                device=device,
                decode_one_token=decode_one_token,
                text=str(item["text"]),
                num_samples=1,
                max_new_tokens=0,
                top_p=float(payload.get("top_p", 0.9)),
                top_k=int(payload.get("top_k", 30)),
                temperature=float(payload.get("temperature", 1.0)),
                compile=False,
                iterative_prompt=True,
                chunk_length=int(payload.get("chunk_length", 300)),
                prompt_text=[reference_text],
                prompt_tokens=[prompt_tokens],
            ):
                if response.action == "sample":
                    codes.append(response.codes)
                elif response.action == "next":
                    break
            if not codes:
                raise RuntimeError(f"Fish S2 did not return audio codes for {item['segment_id']}")
            merged = torch.cat(codes, dim=1).to(device)
            audio = decode_to_audio(merged, codec)
            soundfile.write(str(output), audio.cpu().float().numpy(), codec.sample_rate)
            generated.append({"segment_id": item["segment_id"], "path": str(output), "cache_hit": False})
    finally:
        del codec, model
        gc.collect()
        torch.cuda.empty_cache()
    return {"status": "ok", "engine": "fish_s2", "generated": generated}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text(encoding="utf-8-sig"))
    print(json.dumps(generate_batch(payload), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
