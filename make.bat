@echo off
REM Windows shim so `make <target>` works without GNU make installed.
REM Mirrors the targets in the Makefile.
setlocal
set TARGET=%1
if "%TARGET%"=="" set TARGET=test

if /I "%TARGET%"=="test" (
    python -m pytest -q
    goto :eof
)
if /I "%TARGET%"=="lint" (
    ruff check . && ruff format --check .
    goto :eof
)
if /I "%TARGET%"=="typecheck" (
    mypy
    goto :eof
)
if /I "%TARGET%"=="schemas" (
    echo No schema exporter yet (added in Prompt 2).
    goto :eof
)
echo Unknown target: %TARGET%
echo Valid targets: test lint typecheck schemas
exit /b 1
