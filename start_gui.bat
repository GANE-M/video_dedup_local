@echo off
cd /d "%~dp0"
if exist ".venv-ocr\Scripts\pythonw.exe" (
  start "" ".venv-ocr\Scripts\pythonw.exe" gui.py
) else (
  start "" pythonw gui.py
)
