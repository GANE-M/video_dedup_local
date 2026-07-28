from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
MANIFEST = ROOT / "recap" / "voices" / "fish_s2_role_pack.json"
REQUEST = ROOT / "recap" / "voices" / ".fish_s2_role_preview_request.json"


def main() -> int:
    payload = {
        "checkpoint": str((WORKSPACE / ".model-cache" / "fish-s2-pro-nf4").resolve()),
        "manifest": str(MANIFEST.resolve()),
        "max_seq_len": 4096,
        "seed": 8800,
    }
    REQUEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    python = WORKSPACE / ".tts-envs" / "fish-s2" / "Scripts" / "python.exe"
    result = subprocess.run([str(python), "-m", "recap.fish_profile_previews", str(REQUEST)], cwd=ROOT, env=env)
    REQUEST.unlink(missing_ok=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
