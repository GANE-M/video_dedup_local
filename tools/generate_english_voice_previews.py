from __future__ import annotations

import json
from pathlib import Path

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "recap" / "voices" / "library.json"
PREVIEW_TEXT = (
    "One decision changed everything. Here is how the secret was discovered, "
    "why nobody saw the danger coming, and what happened when the truth finally "
    "came out for everyone involved."
)


def main() -> int:
    payload = json.loads(LIBRARY.read_text(encoding="utf-8-sig"))
    profiles = [
        item for item in payload["voices"]
        if item["voice_id"].startswith("en_")
    ]
    model = Qwen3TTSModel.from_pretrained(
        "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device_map="cuda:0",
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )
    for index, profile in enumerate(profiles, 1):
        reference = (LIBRARY.parent / profile["reference_audio"]).resolve()
        output = (LIBRARY.parent / profile["preview_audio"]).resolve()
        prompt = model.create_voice_clone_prompt(
            ref_audio=str(reference),
            ref_text=profile["reference_text"],
            x_vector_only_mode=False,
        )
        torch.manual_seed(5100 + index)
        torch.cuda.manual_seed(5100 + index)
        wavs, sample_rate = model.generate_voice_clone(
            text=PREVIEW_TEXT,
            language="English",
            voice_clone_prompt=prompt,
            non_streaming_mode=True,
            do_sample=True,
            temperature=0.55,
            top_p=0.82,
            top_k=30,
            repetition_penalty=1.05,
        )
        sf.write(output, wavs[0], sample_rate)
        print(f"{index}/{len(profiles)} {profile['voice_id']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
