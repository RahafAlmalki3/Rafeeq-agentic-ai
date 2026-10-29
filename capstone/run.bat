@echo off
rem Double-click to start Rafeeq: runs the server and opens http://127.0.0.1:8793/ in the browser. Ctrl+C stops it.
title Rafeeq
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul || (
  echo uv is not installed. Install it with:
  echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  pause
  exit /b 1
)

rem open the browser as soon as the server answers
if "%NO_BROWSER%"=="" start "" /b powershell -NoProfile -WindowStyle Hidden -Command ^
  "for ($i = 0; $i -lt 120; $i++) { try { Invoke-WebRequest http://127.0.0.1:8793/healthz -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process 'http://127.0.0.1:8793/'; break } catch { Start-Sleep 1 } }"

uv run rafeeq serve

echo.
echo Rafeeq stopped.
pause
