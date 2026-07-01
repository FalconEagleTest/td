# TDLib — readFileRemotePart fork

A focused fork of [tdlib/td](https://github.com/tdlib/td) that adds zero-disk streaming for Kodi addons running on low-storage Android TV boxes and Raspberry Pi.

## What this fork adds

### `readFileRemotePart`
Streams a byte range directly from Telegram's servers without staging the file to local disk first.

```
readFileRemotePart file_id:int32 offset:int53 count:int32 = Data;
```

Standard TDLib requires `downloadFile` before you can read bytes — that fills the device storage. On a cheap Android TV box with 4–8 GB eMMC (most of it used by Android + apps), a single 1 GB MKV fills the cache. `readFileRemotePart` lets a media player buffer and seek without writing anything to disk.

### CDN `file_hashes` verification
When Telegram redirects a large file to a CDN node, the CDN operator could theoretically tamper with the bytes — AES-CTR provides confidentiality but not integrity. This fork fetches `upload.getCdnFileHashes` from the trusted main DC and SHA-256-verifies every decrypted CDN chunk before delivering it to the caller. Aliaksei Levin (TDLib author) flagged the absence of this check as a security vulnerability in the original PR — it is implemented here.

## Why upstream rejected it

From Aliaksei's review:
> *"Files are supposed to be cached by Telegram apps and aren't supposed to be downloaded again and again over network."*

TDLib's design contract is: download once, cache locally, read from disk. `readFileRemotePart` breaks that contract by re-fetching bytes on every seek. For most users on normal hardware this is the wrong trade-off. For storage-constrained embedded devices (Android TV boxes, Raspberry Pi) it is the only viable option — the disk cache would hold at most one file at a time and get evicted constantly anyway.

## Pre-built binaries

Download from [Releases](../../releases):

| File | Platform |
|---|---|
| `libtdjson-windows.zip` | Windows x64 (MinGW64 + runtime DLLs) |
| `libtdjson-android.zip` | Android arm64-v8a + armeabi-v7a |
| `libtdjson-linux-arm64.so` | Linux ARM64 (Raspberry Pi 4/5, CoreELEC) |
| `libtdjson-linux-armhf.so` | Linux ARMhf (Raspberry Pi 2/3, LibreELEC 32-bit) |

Releases are tagged `vVERSION-YYYYMMDD` (e.g. `v1.8.65-20260701`).

## Using the Android binaries in a Kodi addon

```
plugin.video.yourAddon/
  resources/
    lib/
      libtdjsonjava64.so   ← arm64-v8a (copy from libtdjson-android.zip)
      libtdjsonjava32.so   ← armeabi-v7a
```

Load with ctypes from your service.py — TDLib's JSON interface works identically to upstream.

## Building

Builds run automatically on the `production` branch via GitHub Actions.  
Manual trigger: Actions → **Build TDLib** → Run workflow.

Inputs:
- **Rebase first** — pull upstream `tdlib/td master` before building
- **Build Windows / Android / Linux ARM** — select targets
- **Create release** — tag and publish to GitHub Releases

## Differences from upstream

| | upstream tdlib/td | this fork |
|---|---|---|
| `readFileRemotePart` | ✗ | ✓ |
| CDN `file_hashes` verification | ✓ (in `downloadFile`) | ✓ (in `readFileRemotePart`) |
| tl-parser C11 fix (GCC 15 / Clang 16) | pending | ✓ |
| Pre-built Android arm64+armv7 | ✗ | ✓ via CI |
| Pre-built Windows MinGW64 | ✗ | ✓ via CI |
| Pre-built Linux ARM | ✗ | ✓ via CI |

## Based on

TDLib 1.8.65 — Copyright Aliaksei Levin, Arseny Smirnov.  
Distributed under the Boost Software License 1.0.
