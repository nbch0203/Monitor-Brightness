@echo off
REM Genera dist\Control de Brillo.exe (ejecutable independiente, sin consola)
cd /d "%~dp0"
pip install -r requirements.txt pyinstaller
for /f "delims=" %%i in ('python -c "import customtkinter,os;print(os.path.dirname(customtkinter.__file__))"') do set CTK=%%i
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "Control de Brillo" --icon icono.ico --add-data "%CTK%;customtkinter" --hidden-import pystray._win32 --hidden-import win32timezone --hidden-import wmi brillo.py
rmdir /s /q build
del "Control de Brillo.spec"
echo.
echo Listo: dist\Control de Brillo.exe
pause
