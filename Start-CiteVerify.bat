@echo off
setlocal

rem Start CiteVerify from the folder containing this launcher.
cd /d "%~dp0"

set "PYTHON_CMD=python"
where python >nul 2>&1
if errorlevel 1 set "PYTHON_CMD=py"

if not exist "%~dp0citeverify_web.py" (
    echo CiteVerify could not be found in this folder.
    echo Please keep this launcher inside the CiteVerify folder.
    pause
    exit /b 1
)

rem Prefer portable key files beside the project when they exist.
set "OPENALEX_FILE=%~dp0openalex.txt"
set "S2_FILE=%~dp0S2.txt"

set "KEY_ARGS="
if exist "%OPENALEX_FILE%" set "KEY_ARGS=%KEY_ARGS% --openalex-key-file "%OPENALEX_FILE%""
if exist "%S2_FILE%" set "KEY_ARGS=%KEY_ARGS% --s2-api-key-file "%S2_FILE%""

if not exist "%OPENALEX_FILE%" if not exist "%S2_FILE%" (
    echo No API-key files were found beside this launcher.
    echo CiteVerify will still run, but OpenAlex and Semantic Scholar cross-checks will be skipped.
    echo To enable them, place openalex.txt and S2.txt in this folder.
    echo.
)

echo Starting CiteVerify...
echo Leave this window open while using the browser page.
echo.
%PYTHON_CMD% ".\citeverify_web.py" %KEY_ARGS%

echo.
echo CiteVerify has stopped.
pause
