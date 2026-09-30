@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   ============================================================
echo     SoundScribe  -  local video / audio to text
echo   ============================================================
echo.

set "PY="
if exist "%~dp0runtime\python\python.exe" set "PY=%~dp0runtime\python\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\soundscribe\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\soundscribe\Scripts\python.exe"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

if not defined PY (
  echo   [ERROR] Python runtime not found. Cannot start.
  echo.
  echo   Tried these locations:
  echo     1. .\runtime\python\python.exe
  echo     2. %%USERPROFILE%%\.workbuddy\binaries\python\envs\soundscribe\Scripts\python.exe
  echo     3. .\.venv\Scripts\python.exe
  echo.
  echo   Please send a screenshot of this window.
  echo.
  pause
  exit /b 1
)

echo   runtime : !PY!
echo.
echo   Starting, please wait a few seconds.
echo   Browser will open:  http://127.0.0.1:8765
echo.
echo   IMPORTANT: keep this window open.
echo              It IS the program. Closing it stops everything.
echo              To quit, just close this window.
echo.

"%PY%" -u app\server\main.py --open
set "RC=!ERRORLEVEL!"

echo.
if not "!RC!"=="0" echo   [NOTE] exited with code !RC! -- see messages above
echo.
echo   Stopped. You can close this window now.
echo.
pause
