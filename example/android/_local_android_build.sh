#!/usr/bin/env bash
# Local Android build, runs inside WSL ext4 (NOT /mnt/c which breaks
# on NDK symlinks / case-conflicts).
set -euo pipefail

cd ~/tdbuild/td/example/android

NDK_VER=23.2.8568313
NDK_R=r23c
SDK=SDK

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
  echo "NDK ready at $SDK/ndk/$NDK_VER"
else
  echo "NDK already present at $SDK/ndk/$NDK_VER"
fi

echo
echo "===== [2/4] Build OpenSSL for arm64-v8a + armeabi-v7a ====="
if [ ! -d third-party/openssl ]; then
  ./build-openssl.sh "$SDK"
else
  echo "third-party/openssl already exists - skipping. Delete to rebuild."
fi

echo
echo "===== [3/4] Build TDLib JSON for arm64-v8a + armeabi-v7a ====="
./build-tdlib.sh "$SDK" "" "" "" "JSON"

echo
echo "===== [4/4] Copy outputs back to /mnt/c ====="
DEST=/mnt/c/Users/ezrab/td/example/android/tdlib
rm -rf "$DEST"
mkdir -p "$DEST"
cp -r tdlib/libs "$DEST/"
ls -lh "$DEST/libs"/*/libtdjson.so

echo
echo "Plugin copy (run when ready):"
echo "  cp $DEST/libs/arm64-v8a/libtdjson.so   /mnt/c/Users/ezrab/plugin.video.telemedia/resources/lib/libtdjson_aarch64.so"
echo "  cp $DEST/libs/armeabi-v7a/libtdjson.so /mnt/c/Users/ezrab/plugin.video.telemedia/resources/lib/libtdjson_armv7.so"
