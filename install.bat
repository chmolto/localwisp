@echo off
cd /d "%~dp0"
echo Installing LocalWisp dependencies...
python -m pip install -r requirements.txt || goto :error

where nvidia-smi >nul 2>&1
if %errorLevel% equ 0 (
    echo NVIDIA GPU detected, installing CUDA libraries...
    python -m pip install -r requirements-gpu.txt || goto :error
) else (
    echo No NVIDIA GPU detected: LocalWisp will run on the CPU.
)

echo.
echo Done. Double-click run.bat to start LocalWisp.
pause
exit /b 0

:error
echo.
echo Installation failed. Make sure Python 3.9+ is installed and on PATH.
pause
exit /b 1
