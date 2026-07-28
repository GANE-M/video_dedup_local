from __future__ import annotations

import argparse
from pathlib import Path

import soundfile
import torch
from fish_speech.models.text2semantic.inference import (
    decode_to_audio,
    encode_audio,
    generate_long,
    init_model,
    load_codec_model,
)


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
VOICES = ROOT / "recap" / "voices"
CHECKPOINT = WORKSPACE / ".model-cache" / "fish-s2-pro-nf4"
PREVIEW_TEXT = (
    "في كل قصة سر لا يراه أحد. واليوم نكشف الحقيقة التي غيّرت حياة الجميع، "
    "ونروي كيف بدأ الخطر قبل أن يدرك الأبطال ما كان ينتظرهم."
)
FISH_REFERENCE_TEXT = (
    "في عالم لا يمنح أبطاله فرصة ثانية، تبدأ حكايتنا بقرار واحد يغير كل شيء. "
    "وبين الخيانة والخوف، يكتشف البطل أن النجاة لا تعتمد على القوة وحدها، "
    "بل على الشجاعة والثقة والقدرة على الوقوف حين يختار الجميع الهروب، مهما كان الثمن."
)
ENGLISH_REFERENCE_TEXT = (
    "Every secret has a price. Tonight, one quiet decision will expose the truth, "
    "change two families forever, and force everyone involved to choose between "
    "loyalty, survival, and the person they love most."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    device = "cuda"
    precision = torch.float16
    model, decode_one_token = init_model(
        CHECKPOINT, device, precision, compile=False, max_length=4096, bnb4=True,
    )
    with torch.device(device):
        model.setup_caches(
            max_batch_size=1,
            max_seq_len=model.config.max_seq_len,
            dtype=next(model.parameters()).dtype,
        )
    codec = load_codec_model(CHECKPOINT / "codec.pth", device, precision)
    directories = sorted(path for path in VOICES.glob("ar_*") if path.is_dir())
    for index, directory in enumerate(directories, 1):
        output = directory / "preview.wav"
        if output.is_file() and not args.force:
            print(f"{index}/{len(directories)} cached {directory.name}", flush=True)
            continue
        reference = directory / "reference.wav"
        reference_text = FISH_REFERENCE_TEXT if directory.name == "ar_fish_female" else ENGLISH_REFERENCE_TEXT
        prompt_tokens = encode_audio(reference, codec, device).cpu()
        torch.manual_seed(4100 + index)
        torch.cuda.manual_seed(4100 + index)
        codes = []
        for response in generate_long(
            model=model,
            device=device,
            decode_one_token=decode_one_token,
            text=PREVIEW_TEXT,
            num_samples=1,
            max_new_tokens=0,
            top_p=0.9,
            top_k=30,
            temperature=0.9,
            compile=False,
            iterative_prompt=True,
            chunk_length=300,
            prompt_text=[reference_text],
            prompt_tokens=[prompt_tokens],
        ):
            if response.action == "sample":
                codes.append(response.codes)
            elif response.action == "next":
                break
        if not codes:
            raise RuntimeError(f"no Fish codes returned for {directory.name}")
        audio = decode_to_audio(torch.cat(codes, dim=1).to(device), codec)
        soundfile.write(output, audio.cpu().float().numpy(), codec.sample_rate)
        print(f"{index}/{len(directories)} generated {directory.name}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
