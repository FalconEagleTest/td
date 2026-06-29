#!/usr/bin/env bash
# Local Android build, runs inside WSL ext4 (NOT /mnt/c which breaks
# on NDK symlinks / case-conflicts). Restricts to arm64-v8a + armeabi-v7a
# (Kodi targets); patches build-{openssl,tdlib}.sh on every run so the
# rsync from /mnt/c doesn't undo the ABI restriction.
set -euo pipefail

cd ~/tdbuild/td/example/android

NDK_VER=23.2.8568313
NDK_R=r23c
SDK=SDK
# Where the production batch on Windows expects to find the final .so files.
DEST=/mnt/c/Users/ezrab/td-pr/example/android/tdlib

echo "===== [0/4] Patch ABI loop in build-{openssl,tdlib}.sh ====="
sed -i 's/^for ABI in arm64-v8a armeabi-v7a x86_64 x86 ; do/for ABI in arm64-v8a armeabi-v7a ; do/' \
  build-openssl.sh build-tdlib.sh
# Pre-emptively scrub orphaned x86 build dirs from a previous unpatched run.
rm -rf build-x86_64-JSON build-x86-JSON

echo
echo "===== [1/4] Fetch NDK $NDK_R ====="
if [ ! -d "$SDK/ndk/$NDK_VER" ]; then
  mkdir -p "$SDK/ndk"
  NDK_ZIP=/tmp/android-ndk-$NDK_R-linux.zip
  if [ ! -s "$NDK_ZIP" ]; then
    echo "downloading NDK..."
    rm -f "$NDK_ZIP"
    curl -L --fail --progress-bar \
      -o "$NDK_ZIP" \
      "https://dl.google.com/android/repository/android-ndk-$NDK_R-linux.zip"
  else
    echo "NDK zip already in /tmp ($(du -h "$NDK_ZIP" | cut -f1))"
  fi
  echo "extracting NDK to ext4..."
  unzip -q "$NDK_ZIP" -d "$SDK/ndk"
  mv "$SDK/ndk/android-ndk-$NDK_R" "$SDK/ndk/$NDK_VER"
else
  echo "NDK already at $SDK/ndk/$NDK_VER"
fi

echo
echo "===== [2/4] Build OpenSSL (arm64-v8a + armeabi-v7a) ====="
if [ ! -d third-party/openssl ]; then
  ./build-openssl.sh "$SDK"
else
  echo "third-party/openssl already exists - skipping. rm -rf it to rebuild."
fi

echo
echo "===== [3/4] Build TDLib JSON (incremental, arm64-v8a + armeabi-v7a) ====="
./build-tdlib.sh "$SDK" "" "" "" "JSON"

echo
echo "===== [4/4] Copy outputs to $DEST ====="
rm -rf "$DEST"
mkdir -p "$DEST"
cp -r tdlib/libs "$DEST/"
ls -lh "$DEST/libs"/*/libtdjson.so

echo
echo "Plugin deploy hint:"
echo "  cp $DEST/libs/arm64-v8a/libtdjson.so   /mnt/c/Users/ezrab/plugin.video.telemedia/resources/lib/libtdjsonjava64.so"
echo "  cp $DEST/libs/armeabi-v7a/libtdjson.so /mnt/c/Users/ezrab/plugin.video.telemedia/resources/lib/libtdjsonjava32.so"
