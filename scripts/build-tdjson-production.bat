@echo off
:: --------------------------------------------------------------------
:: Production build: fetch latest upstream/tdlib/td, rebase YOUR patches
:: (tl-parser fix + readFileRemotePart) on top, build Windows + Android,
:: deploy to the Kodi plugin.
::
:: Assumes the sibling-folder layout documented in scripts\README.md:
::   <parent>\td-pr\                    (this repo, worktree on `production`)
::   <parent>\plugin.video.telemedia\   (Kodi addon install target)
:: --------------------------------------------------------------------
setlocal EnableDelayedExpansion

set REPO=%~dp0..
set BUILD=%REPO%\build-mingw64
set PLUGIN_DLL=%REPO%\..\plugin.video.telemedia\resources\lib\x64\tdjson.dll
set PLUGIN_LIB=%REPO%\..\plugin.video.telemedia\resources\lib
set MSYS_BASH=C:\msys64\usr\bin\bash.exe

echo.
echo === [1/6] Fetch upstream ===
cd /d "%REPO%"
git fetch upstream master --quiet
if not !errorlevel! == 0 ( echo FETCH FAILED & exit /b 1 )

echo.
echo === [2/6] Checkout + rebase production on upstream/master ===
git checkout production --quiet
if not !errorlevel! == 0 ( echo CHECKOUT FAILED & exit /b 1 )
git rebase upstream/master
if not !errorlevel! == 0 (
  echo.
  echo ============================================================
  echo REBASE CONFLICT.  Upstream changed something near our patches.
  echo Resolve manually:
  echo   1. cd %REPO%
  echo   2. git status   ^(to see conflicted files^)
  echo   3. Fix the conflict markers
  echo   4. git add ^<files^>
  echo   5. git rebase --continue
  echo   OR abandon: git rebase --abort
  echo ============================================================
  exit /b 1
)

for /f "delims=" %%i in ('"%MSYS_BASH%" -lc "cygpath -u '%BUILD%'"') do set BUILD_MSYS=%%i

echo.
echo === [3/6] Build libtdjson.dll (MinGW64) ===
set MSYSTEM=MINGW64
"%MSYS_BASH%" -lc "cd '%BUILD_MSYS%' && ninja libtdjson.dll"
if not !errorlevel! == 0 ( echo WINDOWS BUILD FAILED & exit /b 1 )

"%MSYS_BASH%" -lc "strip --strip-debug --strip-unneeded '%BUILD_MSYS%/libtdjson.dll'"

echo.
echo === [4/6] Deploy Windows DLL ===
copy /Y "%BUILD%\libtdjson.dll" "%PLUGIN_DLL%"
if not !errorlevel! == 0 ( echo COPY FAILED ^(Kodi running?^) & exit /b 1 )

for /f "delims=" %%i in ('wsl -d Ubuntu wslpath -a "%REPO%"') do set REPO_WSL=%%i

echo.
echo === [5/6] Sync source to WSL + build Android ===
wsl -d Ubuntu -u root -- bash -c "rsync -a --exclude='build-mingw64/' --exclude='.git/' --exclude='example/android/SDK/' --exclude='example/android/third-party/' --exclude='example/android/build-*/' --exclude='example/android/tdlib/' '%REPO_WSL%/' ~/tdbuild/td/"
if not !errorlevel! == 0 ( echo WSL SYNC FAILED & exit /b 1 )

wsl -d Ubuntu -u root -- bash -c "~/tdbuild/td/example/android/_local_android_build.sh"
if not !errorlevel! == 0 ( echo ANDROID BUILD FAILED & exit /b 1 )

echo.
echo === [6/6] Deploy Android .so files ===
copy /Y "%REPO%\example\android\tdlib\libs\arm64-v8a\libtdjson.so"   "%PLUGIN_LIB%\libtdjsonjava64.so"
copy /Y "%REPO%\example\android\tdlib\libs\armeabi-v7a\libtdjson.so" "%PLUGIN_LIB%\libtdjsonjava32.so"

echo.
echo === ALL DONE ===
echo Windows:  %PLUGIN_DLL%
echo Android:  %PLUGIN_LIB%\libtdjsonjava{32,64}.so
echo Verify in Kodi: Settings -^> TDLIB version
endlocal
