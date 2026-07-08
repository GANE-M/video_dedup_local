@echo off
cd /d "%~dp0"
if not exist ".venv-ocr\Scripts\pythonw.exe" (
  echo Missing .venv-ocr. Please run: py -3.12 -m venv .venv-ocr
  pause
  exit /b 1
)
start "" ".venv-ocr\Scripts\pythonw.exe" gui.py
