from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
DEFAULT_PYTHON = WORKSPACE / ".tts-envs" / "fish-s2" / "Scripts" / "python.exe"
DEFAULT_CHECKPOINT = WORKSPACE / ".model-cache" / "fish-s2-pro-nf4"
DEFAULT_OUTPUT = ROOT / "recap" / "voices" / "candidates" / "fish_s2"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Fish Speech S2 voice audition pool without changing the approved library.")
    parser.add_argument("--language", choices=("English", "Arabic"), default="English")
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--start-seed", type=int, default=202600)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--refresh-only", action="store_true")
    args = parser.parse_args()
    output_dir = (args.output_dir or DEFAULT_OUTPUT / args.language.casefold()).resolve()
    payload = {
        "checkpoint": str(args.checkpoint.resolve()),
        "output_dir": str(output_dir),
        "language": args.language,
        "count": args.count,
        "start_seed": args.start_seed,
        "max_seq_len": 4096,
        "temperature": 1.0,
        "top_p": 0.9,
        "top_k": 30,
        "mode": "refresh" if args.refresh_only else "generate",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    request_path = output_dir / "request.json"
    request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run(
        [str(args.python.resolve()), "-m", "recap.fish_voice_candidates", str(request_path)],
        cwd=ROOT,
        env=env,
        text=True,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
