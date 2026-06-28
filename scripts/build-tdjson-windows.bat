@echo off
:: --------------------------------------------------------------------
:: Build the patched TDLib for Windows (MinGW64 / GCC) and deploy the
:: stripped tdjson.dll into the Kodi plugin.
:: Source tree:  C:\Users\ezrab\td
:: Build dir:    C:\Users\ezrab\td\build-mingw64
:: Plugin path:  C:\Users\ezrab\plugin.video.telemedia\resources\lib\x64\tdjson.dll
:: --------------------------------------------------------------------
setlocal

set REPO=C:\Users\ezrab\td
set BUILD=%REPO%\build-mingw64
set PLUGIN_DLL=C:\Users\ezrab\plugin.video.telemedia\resources\lib\x64\tdjson.dll
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

echo.
echo === [1/3] Build libtdjson.dll (MinGW64) ===
set MSYSTEM=MINGW64
"%MSYS_BASH%" -lc "cd /c/Users/ezrab/td/build-mingw64 && ninja libtdjson.dll"
if errorlevel 1 (
  echo BUILD FAILED.
  exit /b 1
)

echo.
echo === [2/3] Strip debug symbols (84 MB -^> ~31 MB) ===
"%MSYS_BASH%" -lc "strip --strip-debug --strip-unneeded /c/Users/ezrab/td/build-mingw64/libtdjson.dll"

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
