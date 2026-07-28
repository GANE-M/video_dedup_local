from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel


ROOT = Path(__file__).resolve().parents[1]
VOICES = ROOT / "recap" / "voices"
FISH_SAMPLE = ROOT / "tts_lab" / "outputs" / "fish_s2_pro_nf4_ar.wav"
REFERENCE_TEXT = (
    "Every secret has a price. Tonight, one quiet decision will expose the truth, "
    "change two families forever, and force everyone involved to choose between "
    "loyalty, survival, and the person they love most."
)

SPECS = {
    "en_bright_female": "Female English narrator in her twenties, bright, agile and friendly. Clear modern social-video delivery, lively but never childish or shrill.",
    "en_dramatic_female": "Female English narrator in her thirties, cinematic and emotionally intense. Controlled suspense, firm consonants and deliberate dramatic pauses.",
    "en_mature_female": "Mature female English narrator, elegant, authoritative and reassuring. Low warm register, measured documentary rhythm and subtle gravity.",
    "en_young_male": "Young male English narrator, energetic, natural and quick-witted. Contemporary short-video pacing, confident without sounding like an announcer.",
    "en_deep_male": "Male English narrator in his forties, deep resonant voice, restrained and cinematic. Slow controlled delivery with a serious dramatic presence.",
    "en_urgent_male": "Male English narrator in his early thirties, tense and urgent but intelligible. Fast thriller pacing, focused energy and short purposeful pauses.",
    "ar_warm_female": "Female narrator in her late twenties, warm, intimate and compassionate. Smooth medium pitch, calm storytelling rhythm and natural emotion.",
    "ar_bright_female": "Young female narrator, bright, energetic and clear. Lively social-video storytelling without becoming childish, nasal or exaggerated.",
    "ar_mature_female": "Mature female narrator, elegant, authoritative and composed. Low warm register, measured pacing and dignified dramatic weight.",
    "ar_calm_male": "Male narrator in his thirties, calm, grounded and conversational. Medium-low pitch, documentary clarity and restrained emotion.",
    "ar_deep_male": "Mature male narrator with a deep resonant cinematic voice. Serious, controlled and steady, with deliberate pauses and strong clarity.",
    "ar_young_male": "Young male narrator, energetic, modern and approachable. Fast but clear short-video pacing with confident natural expression.",
    "ar_epic_male": "Male narrator with an epic fantasy tone, powerful and heroic without shouting. Rich low register, suspenseful rhythm and cinematic authority.",
}


def preview_copy(source: Path, target: Path) -> None:
    audio, sample_rate = sf.read(source)
    minimum = int(sample_rate * 10)
    maximum = int(sample_rate * 14)
    if len(audio) < minimum:
        padding = ((0, minimum - len(audio)),) if audio.ndim == 1 else ((0, minimum - len(audio)), (0, 0))
        audio = np.pad(audio, padding)
    audio = audio[:maximum]
    sf.write(target, audio, sample_rate)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    model = Qwen3TTSModel.from_pretrained(
        "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        device_map="cuda:0",
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )
    for index, (voice_id, instruction) in enumerate(SPECS.items(), 1):
        directory = VOICES / voice_id
        reference = directory / "reference.wav"
        preview = directory / "preview.wav"
        directory.mkdir(parents=True, exist_ok=True)
        if args.force or not reference.is_file():
            torch.manual_seed(3100 + index)
            torch.cuda.manual_seed(3100 + index)
            wavs, sample_rate = model.generate_voice_design(
                text=REFERENCE_TEXT,
                language="English",
                instruct=instruction,
                do_sample=True,
                temperature=0.65,
                top_p=0.9,
            )
            sf.write(reference, wavs[0], sample_rate)
        if voice_id.startswith("en_") and (args.force or not preview.is_file()):
            preview_copy(reference, preview)
        print(f"{index}/{len(SPECS)} {voice_id}: {reference}")
    fish_dir = VOICES / "ar_fish_female"
    fish_dir.mkdir(parents=True, exist_ok=True)
    if not FISH_SAMPLE.is_file():
        raise FileNotFoundError(FISH_SAMPLE)
    shutil.copy2(FISH_SAMPLE, fish_dir / "reference.wav")
    print(f"Fish reference: {fish_dir / 'reference.wav'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
