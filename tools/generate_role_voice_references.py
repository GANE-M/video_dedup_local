from __future__ import annotations

import argparse
import json
from pathlib import Path

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel


ROOT = Path(__file__).resolve().parents[1]
VOICES = ROOT / "recap" / "voices"
MANIFEST = VOICES / "fish_s2_role_pack.json"
REFERENCE_TEXT = (
    "A single choice can change an entire family. Tonight the truth comes out, "
    "old promises are tested, and no one can avoid the decision waiting at the end."
)
ENGLISH_PREVIEW_SHORT = (
    "No one expected the quiet stranger to know the family secret. By morning, "
    "every promise had changed and the truth could no longer stay hidden."
)
ENGLISH_PREVIEW_LONG = (
    "No one expected the quiet stranger to know the family secret. By morning, "
    "every promise had changed and the truth could no longer stay hidden. What "
    "happened next forced every person in the house to choose a side."
)
ARABIC_PREVIEW = (
    "لم يتوقع أحد أن يعرف الغريب الهادئ سر العائلة. ومع حلول الصباح، تغيرت كل الوعود، ولم يعد من الممكن إخفاء الحقيقة."
)


ROLE_SPECS = (
    ("en_playful_young_female", "英语·俏皮灵动年轻女声", "English", "female", "young_adult", "playful_heroine", ["playful", "bright", "youthful"],
     "Young adult female narrator, playful, nimble and charming, with a bright natural smile in the voice. Contemporary and expressive, never shrill or childish."),
    ("en_elder_female", "英语·温厚年长女声", "English", "female", "senior", "wise_matriarch", ["mature", "warm", "wise"],
     "Senior female narrator, warm, wise and dignified, with a gently weathered lower register. Clear patient storytelling, never frail or theatrical."),
    ("en_roguish_young_male", "英语·轻快不羁年轻男声", "English", "male", "young_adult", "roguish_hero", ["roguish", "quick", "confident"],
     "Young adult male narrator, quick-witted, lightly roguish and confident. Agile conversational rhythm with a subtle smile, natural rather than announcer-like."),
    ("en_elder_male", "英语·沉稳年长男声", "English", "male", "senior", "wise_patriarch", ["mature", "steady", "wise"],
     "Senior male narrator with a warm seasoned baritone, calm authority and deliberate pacing. Wise and reassuring, not booming or overly cinematic."),
    ("en_child_girl", "英语·活泼女孩角色声", "English", "female", "child_role", "child_girl", ["child", "bright", "curious"],
     "Fictional child-girl character voice, bright, curious and clearly articulated. Youthful and lively but comfortable to hear, without squealing or imitation of a real child."),
    ("en_child_boy", "英语·机灵男孩角色声", "English", "male", "child_role", "child_boy", ["child", "clever", "energetic"],
     "Fictional child-boy character voice, clever, energetic and clear. Naturally youthful with controlled excitement, without shouting or imitation of a real child."),
    ("ar_playful_young_female", "阿语·俏皮灵动年轻女声", "Arabic", "female", "young_adult", "playful_heroine", ["playful", "bright", "youthful"],
     "Young adult female narrator, playful, nimble and charming, with a bright natural smile in the voice. Contemporary and expressive, never shrill or childish."),
    ("ar_elder_female", "阿语·温厚年长女声", "Arabic", "female", "senior", "wise_matriarch", ["mature", "warm", "wise"],
     "Senior female narrator, warm, wise and dignified, with a gently weathered lower register. Clear patient storytelling, never frail or theatrical."),
    ("ar_roguish_young_male", "阿语·轻快不羁年轻男声", "Arabic", "male", "young_adult", "roguish_hero", ["roguish", "quick", "confident"],
     "Young adult male narrator, quick-witted, lightly roguish and confident. Agile conversational rhythm with a subtle smile, natural rather than announcer-like."),
    ("ar_elder_male", "阿语·沉稳年长男声", "Arabic", "male", "senior", "wise_patriarch", ["mature", "steady", "wise"],
     "Senior male narrator with a warm seasoned baritone, calm authority and deliberate pacing. Wise and reassuring, not booming or overly cinematic."),
    ("ar_child_girl", "阿语·活泼女孩角色声", "Arabic", "female", "child_role", "child_girl", ["child", "bright", "curious"],
     "Fictional child-girl character voice, bright, curious and clearly articulated. Youthful and lively but comfortable to hear, without squealing or imitation of a real child."),
    ("ar_child_boy", "阿语·机灵男孩角色声", "Arabic", "male", "child_role", "child_boy", ["child", "clever", "energetic"],
     "Fictional child-boy character voice, clever, energetic and clear. Naturally youthful with controlled excitement, without shouting or imitation of a real child."),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-only", action="store_true")
    args = parser.parse_args()
    previous = {}
    if MANIFEST.is_file():
        previous = {
            item["voice_id"]: item
            for item in json.loads(MANIFEST.read_text(encoding="utf-8-sig")).get("profiles", [])
        }
    model = None
    if not args.manifest_only:
        model = Qwen3TTSModel.from_pretrained(
            "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
        )
    profiles = []
    for index, (voice_id, display_name, language, gender, age_group, role, styles, instruction) in enumerate(ROLE_SPECS, 1):
        directory = VOICES / voice_id
        directory.mkdir(parents=True, exist_ok=True)
        reference = directory / "reference.wav"
        preview = directory / "preview.wav"
        if not reference.is_file():
            if model is None:
                raise FileNotFoundError(reference)
            torch.manual_seed(7200 + index)
            torch.cuda.manual_seed(7200 + index)
            wavs, sample_rate = model.generate_voice_design(
                text=REFERENCE_TEXT,
                language="English",
                instruct=instruction,
                do_sample=True,
                temperature=0.62,
                top_p=0.88,
            )
            sf.write(reference, wavs[0], sample_rate)
        profiles.append({
            "voice_id": voice_id,
            "display_name": display_name,
            "gender": gender,
            "languages": [language],
            "style": styles,
            "age_group": age_group,
            "role_archetype": role,
            "source_kind": "synthetic_voice_design",
            "review_status": str(previous.get(voice_id, {}).get("review_status") or "pending"),
            "quality_score": previous.get(voice_id, {}).get("quality_score"),
            "reference_audio": f"{voice_id}/reference.wav",
            "reference_text": REFERENCE_TEXT,
            "reference_language": "English",
            "preview_audio": f"{voice_id}/preview.wav",
            "preview_text": (
                ENGLISH_PREVIEW_LONG
                if voice_id in {"en_playful_young_female", "en_child_boy"}
                else ENGLISH_PREVIEW_SHORT if language == "English" else ARABIC_PREVIEW
            ),
            "default_speed": 1.04 if age_group in {"young_adult", "child_role"} else 0.94,
            "target_loudness_mode": "match_source_program",
            "engine": "reference_voice",
            "model_version": "Fish Speech S2 Pro NF4",
            "allowed_engines": ["fish_s2"],
            "generation_parameters": {"temperature": 0.88, "top_p": 0.9, "top_k": 30},
            "design_instruction": instruction,
            "provenance": {"reference_generator": "Qwen3-TTS VoiceDesign", "real_person": False},
        })
        for key in ("preview_metrics", "asr_validation"):
            if key in previous.get(voice_id, {}):
                profiles[-1][key] = previous[voice_id][key]
        print(f"{index}/{len(ROLE_SPECS)} {voice_id}", flush=True)
    MANIFEST.write_text(json.dumps({"schema_version": 1, "profiles": profiles}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(MANIFEST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
