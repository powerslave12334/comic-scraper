@echo off
rem Descarga los comics. Puedes pasar la carpeta destino:
rem   download.bat
rem   download.bat --dest "D:\MiColeccion"
python "%~dp0download.py" %*
pause
