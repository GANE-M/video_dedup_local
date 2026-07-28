from __future__ import annotations

import argparse
import gc
import html
import json
import math
import time
from pathlib import Path
from typing import Any


DEFAULT_TEXTS = {
    "English": (
        "One quiet choice changed everything. No one understood the danger until the truth was already impossible to hide.",
        "She thought the secret would protect her family, but one unexpected visitor forced everyone to face the past.",
    ),
    "Arabic": (
        "بدأت الحكاية بقرار صغير، لكن الحقيقة التي ظهرت بعد ذلك غيّرت حياة الجميع، ولم يعد الهروب ممكناً.",
        "ظنت أن السر سيحمي عائلتها، لكن وصول شخص غير متوقع أجبر الجميع على مواجهة الماضي.",
    ),
}


def _audio_metrics(path: Path) -> dict[str, float]:
    import numpy as np
    import soundfile as sf

    audio, sample_rate = sf.read(str(path), always_2d=False)
    values = np.asarray(audio, dtype=np.float64)
    if values.ndim > 1:
        values = values.mean(axis=1)
    absolute = np.abs(values)
    duration = len(values) / float(sample_rate or 1)
    rms = math.sqrt(float(np.mean(np.square(values)))) if len(values) else 0.0
    peak = float(absolute.max()) if len(values) else 0.0
    clipping_ratio = float(np.mean(absolute >= 0.995)) if len(values) else 0.0
    silence_ratio = float(np.mean(absolute < 0.003)) if len(values) else 1.0
    score = 10.0
    if duration < 8.0 or duration > 20.0:
        score -= 2.0
    if peak < 0.05 or peak >= 0.999:
        score -= 2.0
    if rms < 0.015:
        score -= 1.5
    if clipping_ratio > 0.001:
        score -= 2.0
    if silence_ratio > 0.45:
        score -= 1.5
    return {
        "duration_seconds": round(duration, 3),
        "sample_rate": float(sample_rate),
        "rms": round(rms, 6),
        "peak": round(peak, 6),
        "clipping_ratio": round(clipping_ratio, 6),
        "silence_ratio": round(silence_ratio, 6),
        "technical_score": round(max(0.0, score), 2),
    }


def _write_audition_page(path: Path, candidates: list[dict[str, Any]], language: str) -> None:
    rows = []
    for item in candidates:
        metrics = item["metrics"]
        audio_name = Path(item["reference_audio"]).name
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(item['voice_id'])}</code></td>"
            f"<td><audio controls preload='none' src='{html.escape(audio_name)}'></audio></td>"
            f"<td>{metrics['duration_seconds']:.2f}s</td>"
            f"<td>{metrics['technical_score']:.1f}</td>"
            f"<td>{html.escape(item['reference_text'])}</td>"
            "<td>待填写：性别 / 年龄 / 角色 / 风格</td>"
            "</tr>"
        )
    path.write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Fish S2 {html.escape(language)} 候选试听</title>"
        "<style>body{font-family:system-ui;margin:24px;background:#f6f7fb;color:#202124}"
        "table{border-collapse:collapse;width:100%;background:white}th,td{padding:10px;border:1px solid #ddd;vertical-align:top}"
        "th{background:#eef1ff}audio{width:260px}.note{padding:12px;background:#fff8dc;margin-bottom:16px}</style>"
        f"<h1>Fish Speech S2 · {html.escape(language)} 候选音色</h1>"
        "<div class='note'>技术分只检查时长、音量、静音和削波，不代表自然度。必须试听后再标注角色，8.5 分以上才可晋升正式库。</div>"
        "<table><thead><tr><th>ID</th><th>试听</th><th>时长</th><th>技术分</th><th>参考文本</th><th>人工审核</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></html>",
        encoding="utf-8",
    )


def generate_candidates(payload: dict[str, Any]) -> dict[str, Any]:
    import soundfile as sf
    import torch
    from fish_speech.models.text2semantic.inference import (
        decode_to_audio,
        generate_long,
        init_model,
        load_codec_model,
    )

    checkpoint = Path(payload["checkpoint"]).resolve()
    output_dir = Path(payload["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    language = str(payload.get("language") or "English")
    count = max(1, int(payload.get("count", 8)))
    start_seed = int(payload.get("start_seed", 202600))
    texts = list(payload.get("texts") or DEFAULT_TEXTS.get(language) or DEFAULT_TEXTS["English"])
    if not (checkpoint / "model.pth").is_file() or not (checkpoint / "codec.pth").is_file():
        raise FileNotFoundError(f"Fish S2 checkpoint is incomplete: {checkpoint}")

    device = "cuda"
    precision = torch.float16
    started = time.perf_counter()
    model, decode_one_token = init_model(
        checkpoint, device, precision, compile=False,
        max_length=int(payload.get("max_seq_len", 4096)), bnb4=True,
    )
    with torch.device(device):
        model.setup_caches(
            max_batch_size=1,
            max_seq_len=model.config.max_seq_len,
            dtype=next(model.parameters()).dtype,
        )
    codec = load_codec_model(checkpoint / "codec.pth", device, precision)
    candidates: list[dict[str, Any]] = []
    try:
        for index in range(count):
            seed = start_seed + index
            voice_id = f"fish_{language.casefold()[:2]}_{seed}"
            output = output_dir / f"{voice_id}.wav"
            text = texts[index % len(texts)]
            cache_hit = output.is_file()
            if not cache_hit:
                torch.manual_seed(seed)
                torch.cuda.manual_seed(seed)
                codes = []
                for response in generate_long(
                    model=model,
                    device=device,
                    decode_one_token=decode_one_token,
                    text=text,
                    num_samples=1,
                    max_new_tokens=0,
                    top_p=float(payload.get("top_p", 0.9)),
                    top_k=int(payload.get("top_k", 30)),
                    temperature=float(payload.get("temperature", 1.0)),
                    compile=False,
                    iterative_prompt=True,
                    chunk_length=int(payload.get("chunk_length", 300)),
                    prompt_text=None,
                    prompt_tokens=None,
                ):
                    if response.action == "sample":
                        codes.append(response.codes)
                    elif response.action == "next":
                        break
                if not codes:
                    raise RuntimeError(f"Fish S2 did not return audio codes for seed {seed}")
                audio = decode_to_audio(torch.cat(codes, dim=1).to(device), codec)
                sf.write(str(output), audio.cpu().float().numpy(), codec.sample_rate)
            candidates.append({
                "voice_id": voice_id,
                "language": language,
                "seed": seed,
                "reference_text": text,
                "reference_audio": str(output),
                "engine": "fish_s2",
                "model_version": "Fish Speech S2 Pro NF4",
                "source_kind": "synthetic_seed",
                "review_status": "pending",
                "cache_hit": cache_hit,
                "metrics": _audio_metrics(output),
                "suggested_labels": {
                    "gender": "pending_listen",
                    "age_group": "pending_listen",
                    "role_archetype": "pending_listen",
                    "style": [],
                },
            })
    finally:
        del codec, model
        gc.collect()
        torch.cuda.empty_cache()

    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "generator": "Fish Speech S2 Pro NF4",
        "language": language,
        "generated_at_unix": int(time.time()),
        "generation_seconds": round(time.perf_counter() - started, 3),
        "candidates": candidates,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    audition_page = output_dir / "index.html"
    _write_audition_page(audition_page, candidates, language)
    return {"status": "ok", "manifest": str(manifest), "audition_page": str(audition_page), "candidates": candidates}


def refresh_candidates(payload: dict[str, Any]) -> dict[str, Any]:
    output_dir = Path(payload["output_dir"]).resolve()
    manifest = output_dir / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    candidates = list(data.get("candidates") or [])
    for item in candidates:
        item["metrics"] = _audio_metrics(Path(item["reference_audio"]))
    data["candidates"] = candidates
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    audition_page = output_dir / "index.html"
    _write_audition_page(audition_page, candidates, str(data.get("language") or payload.get("language") or ""))
    return {"status": "ok", "manifest": str(manifest), "audition_page": str(audition_page), "candidates": candidates}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an auditable Fish S2 synthetic voice candidate pool.")
    parser.add_argument("payload", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text(encoding="utf-8-sig"))
    result = refresh_candidates(payload) if payload.get("mode") == "refresh" else generate_candidates(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
