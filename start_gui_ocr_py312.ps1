$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pythonw = Join-Path $PSScriptRoot ".venv-ocr\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "未找到 .venv-ocr。请先运行：py -3.12 -m venv .venv-ocr"
}

Start-Process -FilePath $pythonw -ArgumentList ".\gui.py" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
