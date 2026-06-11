@echo off
echo ========================================
echo   ChatPoE - Build EXE
echo ========================================
echo.

REM Check if PyInstaller is installed
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

echo Building poe2-chat.exe ...
echo.

pyinstaller --noconfirm --onefile --windowed ^
    --name "poe2-chat" ^
    --add-data "ui;ui" ^
    --hidden-import=google.generativeai ^
    --hidden-import=mcp ^
    --hidden-import=mcp.client.stdio ^
    --hidden-import=mcp.client.session_group ^
    --hidden-import=mcp.server ^
    --hidden-import=webview ^
    --hidden-import=keyring ^
    --hidden-import=keyring.backends ^
    main.py

echo.
if exist "dist\poe2-chat.exe" (
    echo ========================================
    echo   SUCCESS! EXE created at:
    echo   dist\poe2-chat.exe
    echo ========================================
    echo.
    echo NOTE: The MCP server (poe2-mcp) must be
    echo installed separately via: pip install poe2-mcp
) else (
    echo BUILD FAILED - check errors above
)

pause
