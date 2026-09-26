# stream_test.py

End-to-end test for the `readFileRemotePart` streaming API we added to TDLib.

## Setup

The script loads `libtdjson.dll` from `../build-mingw64/` (the MinGW build).
Make sure that build is complete. Pass `--dll <path>` to override.

## First run (login)

```
python stream_test.py
```

You'll be prompted for phone number, login code, and 2FA password (if set).
Auth is persisted in `./session/tdlib_db/` so subsequent runs skip this.

## Stream a file

Either from the menu:
```
python stream_test.py
> 1
Telegram message link: https://t.me/c/1234567890/100
```

or directly:
```
python stream_test.py https://t.me/c/1234567890/100 --bench
```

Cached link resolutions live in `./session/link_cache.json` — repeat
runs against the same link skip the `getMessageLinkInfo` round-trip.

Downloaded bytes land in `./downloads/<file_name>`.

## What this tests

Every chunk goes through `readFileRemotePart` → our new
`StreamGetFileActor` → MTProto `upload.getFile`. There's **no**
`downloadFile` and no disk persistence on the TDLib side — the bytes
are returned base64-encoded in the JSON response, decoded here, and
written to disk only by this script.

This is the codepath that fixes:
- CDN-redirect crash (now returns a clean error)
- `FILE_REFERENCE_EXPIRED` (auto-refreshes and retries up to 3×)
- MTProto offset/limit alignment (enforced; reject early)
- The 64 KB cap (raised to 1 MiB)
