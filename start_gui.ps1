$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$pythonw = Join-Path $PSScriptRoot ".venv-ocr\Scripts\pythonw.exe"
if (Test-Path -LiteralPath $pythonw) {
    Start-Process -FilePath $pythonw -ArgumentList ".\gui.py" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
} else {
    Start-Process -FilePath "pythonw" -ArgumentList ".\gui.py" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}
