@echo off
REM Desktop launcher: restart server and open Overview
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-desktop.ps1"
