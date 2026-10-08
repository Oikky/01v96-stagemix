@echo off
chcp 65001 >nul
title 01V96 StageMix
cd /d "%~dp0"
echo ==============================================
echo   01V96 StageMix - ponte celular/tablet ^<-^> mesa
echo   Deixe esta janela aberta durante o culto.
echo ==============================================
"%~dp0python\python.exe" "%~dp0servidor.py" %*
pause
