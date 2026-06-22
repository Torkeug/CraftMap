@echo off
echo Building SpaceCraft.exe...
python -m PyInstaller --onefile --noconsole --name SpaceCraft --clean overlay.py
if %ERRORLEVEL% == 0 (
    copy /y dist\SpaceCraft.exe SpaceCraft.exe >nul
    echo Done! SpaceCraft.exe updated.
) else (
    echo Build failed.
)
pause
