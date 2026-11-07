@echo off
rem Open Skies one-time setup. Safe to run again: every step checks first and skips what is done.
rem Everything goes inside this folder (a private Python, packages, downloads); nothing needs admin rights.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist "cache\temp" mkdir "cache\temp"
set "TEMP=%~dp0cache\temp"
set "TMP=%~dp0cache\temp"
set "PIP_CACHE_DIR=%~dp0cache\pip"
set PIP_DISABLE_PIP_VERSION_CHECK=1

echo Open Skies setup
echo ================
if not exist ".python\tools\python.exe" call :portable_python
if not exist ".python\tools\python.exe" goto :fail
if not exist ".venv\Scripts\python.exe" echo Creating the Python environment...
if not exist ".venv\Scripts\python.exe" ".python\tools\python.exe" -m venv .venv
if not exist ".venv\Scripts\python.exe" goto :fail
echo Installing Python packages - about 250 MB the first time, a few minutes...
".venv\Scripts\python.exe" -m pip install -q --prefer-binary -r requirements.txt
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" openskies_setup.py %*
if errorlevel 1 goto :fail
echo.
if not "%OPENSKIES_NO_PAUSE%"=="1" pause
exit /b 0

:fail
echo.
echo Setup stopped with an error - see the messages above, and logs\setup.log.
echo Running Setup.bat again carries on where it stopped.
if not "%OPENSKIES_NO_PAUSE%"=="1" pause
exit /b 1

:portable_python
rem The official python.org build published as a NuGet package: a plain zip with venv and pip.
rem Its fingerprint is checked before it is unpacked, and it stays inside this folder.
echo Downloading a private copy of Python 3.13 (about 14 MB)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; $z='cache\python.zip'; Invoke-WebRequest -UseBasicParsing 'https://api.nuget.org/v3-flatcontainer/python/3.13.7/python.3.13.7.nupkg' -OutFile $z; if ((Get-FileHash $z -Algorithm SHA256).Hash -ne 'e74272a824e23702dfb5f3e11c3660ceabac7487e3366d4551391db5cd762853') { Remove-Item $z; throw 'The Python download did not match its fingerprint.' }; if (Test-Path '.python') { Remove-Item -Recurse -Force '.python' }; Expand-Archive $z '.python'; Remove-Item $z"
exit /b 0
