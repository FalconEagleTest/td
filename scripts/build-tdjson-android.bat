@echo off
:: --------------------------------------------------------------------
:: Build the patched TDLib for Android (arm64 + armv7, JSON interface)
:: inside WSL Ubuntu, then deploy the .so files into the Kodi plugin.
::
:: Assumes the sibling-folder layout documented in scripts\README.md:
::   <parent>\td\                       (this repo)
::   <parent>\plugin.video.telemedia\   (Kodi addon install target)
:: Build tree inside WSL:   /root/tdbuild/td  (rsync'd from Windows)
::                          /root/tdbuild/td/example/android/SDK
::                          /root/tdbuild/td/example/android/third-party/openssl
:: Plugin paths (Android):  resources\lib\libtdjsonjava{32,64}.so
:: --------------------------------------------------------------------
setlocal

set REPO=%~dp0..
set PLUGIN_LIB=%REPO%\..\plugin.video.telemedia\resources\lib
set ARM64_SO=%REPO%\example\android\tdlib\libs\arm64-v8a\libtdjson.so
set ARMV7_SO=%REPO%\example\android\tdlib\libs\armeabi-v7a\libtdjson.so

for /f "delims=" %%i in ('wsl -d Ubuntu wslpath -a "%REPO%"') do set REPO_WSL=%%i

:: WSL Ubuntu must be installed with build deps + NDK already set up.
wsl -d Ubuntu -u root -- bash -c "[ -d /root/tdbuild/td ]"
if errorlevel 1 (
  echo ERROR: WSL build tree at ~/tdbuild/td not initialized.
  echo   First time setup -- run these inside WSL Ubuntu as root:
  echo     mkdir -p ~/tdbuild
  echo     rsync -a --exclude=build-mingw64/ --exclude=.git/ "%REPO_WSL%/" ~/tdbuild/td/
  echo     ~/tdbuild/td/example/android/_local_android_build.sh
  exit /b 1
)

echo.
echo === [1/3] Sync source to WSL (incremental, preserves build cache) ===
wsl -d Ubuntu -u root -- bash -c "rsync -a --exclude='build-mingw64/' --exclude='.git/' --exclude='example/android/SDK/' --exclude='example/android/third-party/' --exclude='example/android/build-*/' --exclude='example/android/tdlib/' '%REPO_WSL%/' ~/tdbuild/td/"
if errorlevel 1 (
  echo SYNC FAILED.
  exit /b 1
)

echo.
echo === [2/3] Incremental Android build (arm64 + armv7, JSON, auto-stripped) ===
wsl -d Ubuntu -u root -- bash -c "~/tdbuild/td/example/android/_local_android_build.sh"
if errorlevel 1 (
  echo BUILD FAILED.
  exit /b 1
)

echo.
echo === [3/3] Deploy to plugin ===
if not exist "%ARM64_SO%" (
  echo ERROR: arm64 .so not produced at %ARM64_SO%
  exit /b 1
)
if not exist "%ARMV7_SO%" (
  echo ERROR: armv7 .so not produced at %ARMV7_SO%
  exit /b 1
)
copy /Y "%ARM64_SO%" "%PLUGIN_LIB%\libtdjsonjava64.so"
copy /Y "%ARMV7_SO%" "%PLUGIN_LIB%\libtdjsonjava32.so"

echo.
echo === DONE ===
echo Deployed:
echo   %PLUGIN_LIB%\libtdjsonjava64.so  (arm64-v8a)
echo   %PLUGIN_LIB%\libtdjsonjava32.so  (armeabi-v7a)
echo Verify in Kodi (on Android): Settings -^> TDLIB version
endlocal
