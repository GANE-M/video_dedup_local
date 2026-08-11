$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv-asr\Scripts\python.exe"

Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath $python)) {
        py -3.12 -m venv (Join-Path $projectRoot ".venv-asr")
    }

    & $python -m pip install -r (Join-Path $projectRoot "requirements-asr.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "ASR 独立环境安装失败，退出码 $LASTEXITCODE"
    }

    & $python -c "import subtitle_tool; from faster_whisper import WhisperModel; print('device=', subtitle_tool.resolve_whisper_device('cuda')); WhisperModel('medium', device='cuda', compute_type='float16', local_files_only=True); print('Whisper CUDA ready')"
    if ($LASTEXITCODE -ne 0) {
        throw "Whisper CUDA 自检失败，退出码 $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
