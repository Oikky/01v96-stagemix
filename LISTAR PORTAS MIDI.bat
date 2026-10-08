@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0python\python.exe" "%~dp0servidor.py" --listar
echo.
echo Se a 01V96 nao aparecer: cabo USB, mesa ligada, driver instalado (pasta drivers).
echo Pra usar uma porta especifica: INICIAR.bat --midi NUMERO
pause
