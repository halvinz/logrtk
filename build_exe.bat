@echo off
REM Build du .exe Windows en un clic.
REM Prerequis : Python 3.10+ installe sur la machine Windows (python.org),
REM avec la case "Add python.exe to PATH" cochee a l'installation.

cd /d "%~dp0"

echo === Creation de l'environnement virtuel ===
python -m venv venv
call venv\Scripts\activate.bat

echo === Installation des dependances ===
pip install --upgrade pip
pip install -r requirements.txt

echo === Compilation du .exe (peut prendre 1 a 3 minutes) ===
pyinstaller --noconfirm --onefile --windowed ^
    --name "RobotLogViewer" ^
    --paths app ^
    app\main.py

echo.
echo === Termine ===
echo Le fichier RobotLogViewer.exe se trouve dans le dossier dist\
pause
