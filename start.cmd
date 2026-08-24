@echo off
setlocal
cd /d "%~dp0"
title AutoCoding Engineer Client

set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%POWERSHELL_EXE%" set "POWERSHELL_EXE=powershell.exe"

"%POWERSHELL_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo AutoCoding Engineer stopped with exit code %EXIT_CODE%.
    echo Review the error above, then press any key to close this window.
    pause >nul
)

exit /b %EXIT_CODE%
