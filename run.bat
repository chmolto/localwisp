@echo off
:: Options for LocalWisp (see README or `python localwisp.py --help`), e.g.:
::   set "LOCALWISP_ARGS=--model small --language en --hotkey f9"
set "LOCALWISP_ARGS="

:: Re-launch as Administrator so the hotkey also works inside elevated windows
net session >nul 2>&1
if %errorLevel% neq 0 (
    powershell -NoProfile -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:: pythonw = no console window
cd /d "%~dp0"
start "" pythonw "%~dp0localwisp.py" %LOCALWISP_ARGS%
