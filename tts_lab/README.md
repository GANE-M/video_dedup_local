# Arabic TTS local comparison

This lab compares four locally runnable Arabic speech models on the same
short-drama narration text. It is isolated from the project's OCR and Whisper
environment.

## Models

| Model | Intended Arabic | Voice source | Test setting |
| --- | --- | --- | --- |
| Chatterbox Multilingual V3 | Multilingual Arabic / MSA | Built-in default or a reference clip | `language_id=ar` |
| SILMA TTS v1 | Fusha / MSA and English | Bundled Arabic reference or a reference clip | automatic tashkeel, speed `1.3` |
| VoiceTut-TTS | Egyptian Arabic and code-switching | 17 built-in voices or a reference clip | built-in `Asmaa`, speed `0.95` |
| Fish Speech S2 Pro NF4 | Tier-2 Arabic support | Random voice or reference-audio cloning | community NF4 4-bit checkpoint, max sequence length `4096` |

## Test text

The UTF-8 source is in `sample_arabic.txt`.

> في عالم لا يمنح أبطاله فرصة ثانية، تبدأ حكايتنا بقرار واحد يغير كل شيء. وبين الخيانة والخوف، يكتشف البطل أن النجاة لا تعتمد على القوة وحدها، بل على الشجاعة والثقة والقدرة على الوقوف حين يختار الجميع الهروب، مهما كان الثمن.

## Measured results on RTX 5080 16 GB

| Model | Audio duration | Generate time | RTF | Peak CUDA memory | Whisper similarity |
| --- | ---: | ---: | ---: | ---: | ---: |
| Chatterbox V3 | 16.44 s | 10.27 s | 0.624 | 3433 MB | 0.984 |
| SILMA | 19.60 s | 2.55 s | 0.130 | 2908 MB | 0.916 |
| VoiceTut / Asmaa | 15.61 s | 1.89 s | 0.121 | 2218 MB | 0.977 |
| Fish S2 Pro NF4 | 20.02 s | 120.32 s incl. 50.21 s load | 6.011 | 8872 MB total GPU use* | 0.989 |

`*` Windows WDDM did not expose reliable per-process GPU memory. The Fish row
records the maximum total GPU memory observed during inference, including the
desktop and other applications.

Whisper similarity is only an automatic intelligibility signal. It does not
measure naturalness, emotion, dialect suitability, or voice attractiveness.

## Local paths

- Python environments: `E:\wangyang\Documents\Codexfile\climind\.tts-envs`
- Model cache: `E:\wangyang\Documents\Codexfile\climind\.model-cache`
- Generated samples and JSON metrics: `tts_lab\outputs`
- Normalized Fish S2 listening copy: `tts_lab\outputs\fish_s2_pro_nf4_ar_preview.mp3`

## Windows compatibility notes

- Chatterbox V3 is installed from the official GitHub repository because the
  current PyPI package does not expose the documented V3 selector.
- SILMA's NeMo number/date normalizer depends on `pynini`, which has no Windows
  wheel. The test disables that optional normalizer but keeps SILMA's Arabic
  tashkeel model enabled. `windows_stubs` only satisfies the unused import.
- SILMA reference audio is read through SoundFile because TorchCodec expects a
  shared-FFmpeg Windows build, while this computer has the common static build.
- VoiceTut's upstream loader downloads training checkpoints by default. The
  test filters optimizer, scheduler, and random-state files because they are
  not needed for inference.
- Fish S2 Pro was tested through the community
  `groxaxo/s2-pro-BnB-4Bits` checkpoint and `fish-speech-int4-patch`, because
  the official BF16 S2 Pro path recommends 24 GB VRAM and Linux/WSL. The local
  tokenizer metadata required a compatibility correction for Transformers
  4.57.3. This result is not an official BF16 benchmark.
- Fish Speech S2 Pro uses the Fish Audio Research License. Commercial use
  requires a separate license from Fish Audio.
- The first three tests use isolated Python 3.11 environments; Fish S2 uses
  an isolated Python 3.12 environment with CUDA PyTorch 2.8.0.

Only clone a real person's voice with their explicit permission.
