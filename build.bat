@echo off
echo Building CraftMap.exe...
.venv\Scripts\python.exe -m PyInstaller --onefile --noconsole --name CraftMap --clean --distpath . overlay.py
if %ERRORLEVEL% == 0 (
    echo Done! CraftMap.exe updated.
) else (
    echo Build failed.
)
pause
