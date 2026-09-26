# Build scripts

These `.bat` files assume a sibling-folder layout — this repo and the
Kodi plugin living next to each other in the same parent directory:

```
<parent>\td-pr\                 (this repo, on `production` branch)
<parent>\td\                    (experimental fork, separate — optional)
<parent>\plugin.video.telemedia\   (Kodi addon install target)
C:\msys64\                      (MinGW64 build toolchain)
WSL distro "Ubuntu"             (Android NDK build environment)
```

Paths are derived automatically from each script's own location
(`%~dp0..`) plus that sibling-folder assumption — no editing needed if
your layout matches. If it doesn't, edit the `REPO=`/`PLUGIN_*=` lines at
the top of each batch file. For the Android WSL build specifically, set
the `TD_WIN_REPO` (and optionally `TD_WIN_PLUGIN_LIB`) environment
variable before running `example/android/_local_android_build.sh`
directly inside WSL.

For the canonical day-to-day flow:

```
scripts\build-tdjson-production.bat
```

End-to-end: fetch upstream → rebase your patches → build Windows DLL +
Android arm64/armv7 .so → strip → deploy to the Kodi plugin.
