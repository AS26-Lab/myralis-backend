@echo off
setlocal
powershell -ExecutionPolicy Bypass -File "%~dp0build_backend.ps1"
endlocal

