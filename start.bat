@echo off
REM Start the bot on Windows: build the venv if it isn't there, install
REM requirements when they've changed, run the bot. The bot itself handles the
REM token -- it reads %DISCORD_TOKEN%, else its saved config, else prompts you.
REM
REM Kept deliberately dumb and readable: anyone should be able to see exactly
REM what this does to their machine before running it.
setlocal
cd /d "%~dp0"

set PYTHON=py -3
%PYTHON% --version >nul 2>&1
if errorlevel 1 set PYTHON=python
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
  echo Python 3 not found. Install it from https://python.org ^(check "Add
  echo Python to PATH" during setup^) and try again.
  exit /b 1
)

if not exist venv (
  echo Creating virtual environment...
  %PYTHON% -m venv venv
  if errorlevel 1 exit /b 1
)

REM Reinstall only when requirements.txt is newer than the last successful
REM install, so a normal start doesn't wait on pip. xcopy /d /l lists the
REM source only when it is newer than the destination -- findstr then tells us
REM whether anything was listed.
set STAMP=venv\.requirements-stamp
set NEEDINSTALL=0
if not exist "%STAMP%" set NEEDINSTALL=1
if exist "%STAMP%" (
  xcopy /d /y /l requirements.txt "%STAMP%" 2>nul | findstr /i "requirements.txt" >nul && set NEEDINSTALL=1
)

if "%NEEDINSTALL%"=="1" (
  echo Installing dependencies...
  venv\Scripts\python.exe -m pip install --quiet --upgrade pip
  venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
  if errorlevel 1 exit /b 1
  echo.> "%STAMP%"
)

venv\Scripts\python.exe bot.py
