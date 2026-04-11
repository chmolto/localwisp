@echo off
:: Auto-elevación a Administrador
net session >nul 2>&1
if %errorLevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:: Lanzar sin ventana de consola extra (pythonw = sin terminal)
start "" pythonw "%~dp0wisp_local.py"
