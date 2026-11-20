@echo off
setlocal
cd /d "%~dp0"
title MarktRadar

set "PYTHON="
if exist ".venv\Scripts\python.exe" goto run
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do if exist "%%~fD\python.exe" set "PYTHON=%%~fD\python.exe"
if not defined PYTHON for %%P in (py.exe python.exe) do if not defined PYTHON (where %%P >nul 2>nul && set "PYTHON=%%P")
if not defined PYTHON (
  echo Python 3.12 or later is required.
  echo Download: https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
"%PYTHON%" -m venv .venv
if errorlevel 1 goto setup_error

:run
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 12))"
if errorlevel 1 (
  echo MarktRadar needs Python 3.12 or later. Delete the .venv folder after updating Python.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" server.py --open
if errorlevel 1 pause
goto end

:setup_error
echo Setup failed. Check your Python installation.
pause

:end
endlocal
