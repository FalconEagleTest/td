@echo off
:: --------------------------------------------------------------------
:: Build the patched TDLib for Windows (MinGW64 / GCC) and deploy the
:: stripped tdjson.dll into the Kodi plugin.
:: Assumes the sibling-folder layout documented in scripts\README.md:
::   <parent>\td\                       (this repo)
::   <parent>\plugin.video.telemedia\   (Kodi addon install target)
:: --------------------------------------------------------------------
setlocal

set REPO=%~dp0..
set BUILD=%REPO%\build-mingw64
set PLUGIN_DLL=%REPO%\..\plugin.video.telemedia\resources\lib\x64\tdjson.dll
set MSYS_BASH=C:\msys64\usr\bin\bash.exe

if not exist "%MSYS_BASH%" (
  echo ERROR: MSYS2 not found at C:\msys64 -- install it first.
  exit /b 1
)
if not exist "%BUILD%\build.ninja" (
  echo ERROR: build dir %BUILD% not configured. Run cmake once first:
  echo   from MSYS2 MINGW64 shell: cd %BUILD% ^&^& cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ..
  exit /b 1
)

for /f "delims=" %%i in ('"%MSYS_BASH%" -lc "cygpath -u '%BUILD%'"') do set BUILD_MSYS=%%i

echo.
echo === [1/3] Build libtdjson.dll (MinGW64) ===
set MSYSTEM=MINGW64
"%MSYS_BASH%" -lc "cd '%BUILD_MSYS%' && ninja libtdjson.dll"
if errorlevel 1 (
  echo BUILD FAILED.
  exit /b 1
)

echo.
echo === [2/3] Strip debug symbols (84 MB -^> ~31 MB) ===
"%MSYS_BASH%" -lc "strip --strip-debug --strip-unneeded '%BUILD_MSYS%/libtdjson.dll'"

echo.
echo === [3/3] Deploy to plugin ===
copy /Y "%BUILD%\libtdjson.dll" "%PLUGIN_DLL%"
if errorlevel 1 (
  echo COPY FAILED -- is Kodi running? (it locks the DLL)
  echo   - Close Kodi, then re-run this batch.
  exit /b 1
)

echo.
echo === DONE ===
echo Deployed: %PLUGIN_DLL%
echo Verify in Kodi: Settings -^> TDLIB version
endlocal
