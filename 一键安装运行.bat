@echo off
title YouTube Subtitle Tool - Setup and Run
echo =====================================================
echo  YouTube Subtitle Tool - one-click setup
echo  First run installs dependencies, please wait a few minutes.
echo  After that, double-click this file to open the tool.
echo =====================================================
echo.
set PYCMD=python
where python >nul 2>nul
if not errorlevel 1 goto PY_OK
where py >nul 2>nul
if not errorlevel 1 goto USE_PY
goto NO_PYTHON
:USE_PY
set PYCMD=py -3
:PY_OK
%PYCMD% --version
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto MAKE_VENV
goto INSTALL_DEPS
:NO_PYTHON
echo [FAILED] Python is not installed, or not in PATH.
echo Please install Python 3.11 or 3.12 from https://www.python.org/downloads/
echo IMPORTANT: check "Add Python to PATH" during installation.
echo After installing, close this window and double-click this file again.
pause
exit /b 1
:MAKE_VENV
echo [1/4] Creating virtual environment .venv ...
%PYCMD% -m venv .venv
if errorlevel 1 goto VENV_FAIL
:INSTALL_DEPS
echo [2/4] Installing dependencies (yt-dlp / faster-whisper) ...
.venv\Scripts\python.exe -m pip install -q --upgrade pip
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if errorlevel 1 goto PIP_FAIL
echo [3/4] Checking ffmpeg ...
where ffmpeg >nul 2>nul
if not errorlevel 1 goto FFMPEG_OK
echo ffmpeg not found, installing a bundled one (imageio-ffmpeg) ...
.venv\Scripts\python.exe -m pip install -q imageio-ffmpeg
:FFMPEG_OK
echo ffmpeg OK.
echo [4/4] Self-test (no internet needed) ...
.venv\Scripts\python.exe yt_subtitle.py --self-test
if errorlevel 1 goto TEST_FAIL
echo.
echo Setup done, opening the tool ...
.venv\Scripts\python.exe yt_subtitle.py
goto END
:VENV_FAIL
echo [FAILED] Could not create the virtual environment.
echo Python may be broken. Try reinstalling Python with "Add Python to PATH" checked.
pause
exit /b 1
:PIP_FAIL
echo [FAILED] Dependency install failed, probably a network problem.
echo Please double-click this file again to retry.
pause
exit /b 1
:TEST_FAIL
echo [FAILED] Self-test did not pass. Please send me the error text above.
pause
exit /b 1
:END
pause
