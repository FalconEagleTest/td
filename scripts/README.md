# Build scripts (personal setup)

These `.bat` files assume a specific local layout:

```
C:\Users\ezrab\td-pr\          (this repo, on `production` branch)
C:\Users\ezrab\td\             (experimental fork, separate)
C:\Users\ezrab\plugin.video.telemedia\   (Kodi addon install target)
C:\msys64\                     (MinGW64 build toolchain)
WSL distro "Ubuntu"            (Android NDK build environment)
```

If your layout differs, edit the `REPO=`, `BUILD=`, `PLUGIN_*=` paths at
the top of each batch file. For the canonical day-to-day flow:

```
scripts\build-tdjson-production.bat
```

End-to-end: fetch upstream → rebase your patches → build Windows DLL +
Android arm64/armv7 .so → strip → deploy to the Kodi plugin.

See `TDLIB_STREAMING_PROJECT.md` in the repo root for full context.
