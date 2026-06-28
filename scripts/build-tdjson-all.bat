@echo off
:: --------------------------------------------------------------------
:: Build both platforms back-to-back. Windows first (~30s incremental,
:: blocks if Kodi has the DLL locked), then Android (~5-10 min incremental
:: rebuild inside WSL).
:: --------------------------------------------------------------------
setlocal

call "%~dp0build-tdjson-windows.bat"
if errorlevel 1 (
  echo Windows build failed -- skipping Android.
  exit /b 1
)

echo.
echo --------------------------------------------------------------------
echo.

call "%~dp0build-tdjson-android.bat"
if errorlevel 1 (
  echo Android build failed.
  exit /b 1
)

echo.
echo === ALL BUILDS DONE ===
endlocal
