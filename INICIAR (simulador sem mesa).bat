@echo off
chcp 65001 >nul
title 01V96 StageMix - SIMULADOR
cd /d "%~dp0"
echo Modo simulador: uma 01V96 de mentira, pra testar a pagina sem a mesa.
start "" http://127.0.0.1:8096
"%~dp0python\python.exe" "%~dp0servidor.py" --simular %*
pause
