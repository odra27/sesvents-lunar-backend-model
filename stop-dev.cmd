@echo off
rem Matikan server backend LUNAR. Bisa diklik dua kali atau diketik di cmd / PowerShell: stop-dev.cmd
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-dev.ps1"
pause
