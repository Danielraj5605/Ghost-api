@echo off
rem Makes `ghost-api` work in any Command Prompt or PowerShell window, like `claude`.
rem
rem It writes a small launcher, %USERPROFILE%\.local\bin\ghost-api.cmd, that runs this
rem project's virtual environment (no need to activate it). The launcher works from the
rem project folder, so your keys (.env), memory (ghost_api.db) and results (output\) are
rem always the same ones, wherever you type the command. Run this again if you move the
rem project. To remove it, delete the launcher file.

setlocal
for %%I in ("%~dp0..") do set "PROJECT=%%~fI"
set "PYTHON=%PROJECT%\.venv\Scripts\python.exe"
set "BIN=%USERPROFILE%\.local\bin"

if not exist "%PYTHON%" (
    echo Could not find %PYTHON%
    echo Set up the project first: see "Getting started" in README.md.
    exit /b 1
)
if not exist "%BIN%" mkdir "%BIN%"

> "%BIN%\ghost-api.cmd" (
    echo @echo off
    echo rem Launcher made by %PROJECT%\scripts\install-command.cmd
    echo setlocal
    echo cd /d "%PROJECT%"
    echo "%PYTHON%" -m ghost_api %%*
)
echo Installed: %BIN%\ghost-api.cmd

rem Put the folder on the user PATH if it isn't there yet (takes effect in new windows).
powershell -NoProfile -Command ^
  "$p = [Environment]::GetEnvironmentVariable('Path','User');" ^
  "$dir = [Environment]::ExpandEnvironmentVariables('%BIN%');" ^
  "if (($p -split ';') -notcontains $dir) {" ^
  "  [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';' + $dir), 'User');" ^
  "  Write-Output ('Added ' + $dir + ' to your PATH.') }"

echo.
echo Open a new Command Prompt or PowerShell window and type:  ghost-api
