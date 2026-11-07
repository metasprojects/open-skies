@echo off
rem Open Skies: real places as Nuclear Option maps. Double-click to start.
rem First run? Setup.bat runs first: it makes a private Python and checks the game and Unity.
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist "cache\temp" mkdir "cache\temp"
set "TEMP=%~dp0cache\temp"
set "TMP=%~dp0cache\temp"
if not exist ".venv\Scripts\pythonw.exe" (
  call Setup.bat
  if errorlevel 1 exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "openskies.py"
