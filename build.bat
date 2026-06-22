@echo off
echo Building CraftMap.exe...
python -m PyInstaller --onefile --noconsole --name CraftMap --clean overlay.py
if %ERRORLEVEL% == 0 (
    copy /y dist\CraftMap.exe CraftMap.exe >nul
    echo Done! CraftMap.exe updated.
) else (
    echo Build failed.
)
pause
