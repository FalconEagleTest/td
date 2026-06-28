# TDLib Streaming Project — Hand-off & State Doc

Last updated: 2026-06-28.

This doc captures the full state of the TDLib streaming work so you (or
Claude in a future session) can pick up from where we left off without
needing to re-derive context.

---

## TL;DR

You needed `plugin.video.telemedia` (Kodi addon) to stream Telegram
video files **without writing the full file to disk first** — required
for Android tv-boxes with limited storage. We:

1. Added a new **`readFileRemotePart`** API to TDLib that fetches a
   byte range from a remote file on-demand, no local cache.
2. Refactored the plugin to use it (HTTP range → parallel chunks →
   straight to Kodi, no disk).
3. Set up build scripts for Windows + Android.
4. Filed two PRs to upstream tdlib/td.
5. Patched plugin-side TDLib API drift (~10 schema mismatches accumulated
   over old TDLib versions).

Plugin currently runs **patched TDLib 1.8.65** with all our streaming
features + bug fixes. Everything works; PRs are awaiting maintainer
response.

---

## Current Open Items (resume point)

When you come back, these are the things still pending:

| # | Item | Where |
|---|---|---|
| 1 | **Watch upstream PRs for maintainer feedback** | github.com/tdlib/td/pulls — search for FalconEagleTest |
| 2 | Try real-world Kodi playback on Windows AND Android tv box (last validated only via stream_test.py) | Kodi |
| 3 | Decide if any of the deferred plugin enhancements are worth doing (HTTP keep-alive, prefetch cache, etc. — see "Deferred enhancements" below) | service.py |
| 4 | When PRs merge: drop the merged commits from the `production` branch | `C:\Users\ezrab\td-pr` |

---

## What "shipped" looks like

Plugin Kodi addon at `C:\Users\ezrab\plugin.video.telemedia\` with:

- **Patched TDLib** (libtdjson.dll on Windows, libtdjsonjava{32,64}.so on Android)
  containing our `readFileRemotePart` and CDN support.
- **Refactored `service.py`** — HTTP range requests use the new API
  via a 4-worker `ThreadPoolExecutor` sliding window. Sequential disk
  reads, confirmed-ranges tracking, and all the legacy buffer-wait
  machinery deleted.
- **`default.py` schema fixes** — `searchChatMessages topic_id`,
  `forwardMessages` options shape, `getChats chat_list`, etc.
- **Removed `check_free_space` gate** — no disk needed for streaming so
  small-storage Android boxes can play any size file.

---

## Repository / worktree layout

```
C:\Users\ezrab\
├── td\                    Main TDLib repo. Has 49 messy experimental commits,
│                          plus uncommitted polish edits. Tracks
│                          github.com/FalconEagleTest/td (your fork).
│                          Use for hacking/experimenting only.
│
├── td-pr\                 CLEAN PR worktree. Branches:
│                          - production:                upstream + 2 patches (USE THIS)
│                          - tl-parser-c11:             1 commit (PR #1)
│                          - readFileRemotePart-streaming: 1 commit (PR #2)
│                          - master:                    tracks upstream/master
│                          Has its own build dir (build-mingw64/).
│
├── plugin.video.telemedia\  Kodi addon. The thing your tv box loads.
│                            Production target -- DLLs/.so files are deployed here.
│                            Backup folders: _backup_pre_readFileRemotePart/
│
├── android-ndk-r23c.zip   NDK download (cached, also extracted to WSL ~/tdbuild/td/example/android/SDK/)
│
├── build-tdjson-windows.bat   <- builds from td\ (experimental)
├── build-tdjson-android.bat   <- builds from td\ (experimental)
├── build-tdjson-all.bat       <- both of above
├── build-tdjson-production.bat   <- DEFAULT — builds from td-pr\production
└── TDLIB_STREAMING_PROJECT.md    <- this doc

WSL Ubuntu 26.04 (inside the wsl distro):
~/tdbuild/td/                          ext4 mirror of td-pr (rsync'd by production batch)
~/tdbuild/td/example/android/SDK/      Android NDK r23c
~/tdbuild/td/example/android/_local_android_build.sh   Custom build orchestrator
~/tdbuild/td/example/android/build-{openssl,tdlib}.sh  Auto-patched to 2 ABIs on each run
```

---

## How to build (the one thing you need to remember)

```
C:\Users\ezrab\build-tdjson-production.bat
```

End-to-end: `git fetch upstream` → rebase production on upstream/master
→ build Windows libtdjson.dll → strip → deploy → rsync source to WSL
→ build Android arm64+armv7 .so → strip → deploy.

Typical timing:
- Upstream unchanged, no local changes: ~2 min (mostly the rsync + WSL nothing-to-do)
- Upstream moved a little: ~5-10 min
- Upstream moved a lot or kernel/NDK update: 20-30 min

On conflict during rebase, the batch stops with clear recovery instructions.

If you want to build from the **experimental `td\` tree** instead (rarely
needed), use `build-tdjson-all.bat`.

---

## What's in the two upstream PRs

### PR #1 — tl-parser/C11 portability

- Branch: `tl-parser-c11`
- Commit: `91545655e` build: pin tl-parser to C11 for GCC 15 / Clang 16 compatibility
- Files: `td/generate/tl-parser/CMakeLists.txt` (+5)
- Why: GCC 15 defaults to `-std=gnu23` where `()` means "no arguments",
  breaking the vendored `wgetopt.c`'s K&R-style declarations.
- Risk to merge: low. Small portability fix.

### PR #2 — readFileRemotePart

- Branch: `readFileRemotePart-streaming`
- Commit: `18559c128` Add readFileRemotePart: stream a remote byte range without local cache
- Files added:
  - `td/generate/scheme/td_api.tl` (+6)  — TL method definition
  - `td/telegram/Requests.cpp` (+14)      — JSON handler
  - `td/telegram/Requests.h` (+1)         — declaration
  - `td/telegram/files/FileManager.cpp` (+327) — `StreamGetFileActor` + helpers
  - `td/telegram/files/FileManager.h` (+11)    — declarations
- Total: **359 insertions, 0 deletions** (purely additive)
- Implements: upload.getFile fetch, CDN redirect handling (upload.getCdnFile
  + AES-CTR decrypt + upload.reuploadCdnFile flow), file-reference refresh
  on FILE_REFERENCE_EXPIRED, MTProto edge validation.
- Not implemented: file_hashes verification for CDN responses, MSVC build
  validation (CI does this).
- Risk to merge: moderate. Likely feedback rounds on naming / structure /
  test coverage.

PRs were filed from `FalconEagleTest <testebsebs@gmail.com>` identity.

---

## How `readFileRemotePart` actually works

In service.py, `do_GET` (the HTTP handler that Kodi hits for video):

1. Parses the Range header → `(start_range, end_range)`.
2. Plans all chunks upfront as 1 MiB-aligned `(offset, count)` tuples.
3. Creates a `ThreadPoolExecutor(max_workers=4)` (configurable via
   `stream_parallel_workers` setting).
4. Submits the first 4 chunks via `read_file_remote_part(file_id, offset, count)`,
   which goes through TDLib's `post_box`+`wait_response` mechanism →
   `td_send({'@type': 'readFileRemotePart', ...})`.
5. As each chunk's Future resolves (in submission order), writes the
   useful bytes to `self.wfile` (Kodi's TCP socket).
6. Refills the window — submit next chunk, wait for next-in-order Future,
   repeat.

In the C++ DLL, each `readFileRemotePart` call lands in `StreamGetFileActor`:

1. Dispatches `upload.getFile` via `net_query_dispatcher`.
2. On `upload.file` response: pass bytes through to caller's Promise.
3. On `upload.fileCdnRedirect` response: cache the CDN token / DC /
   AES key+IV, re-dispatch `upload.getCdnFile` to the CDN DC.
4. On `upload.cdnFile` response: AES-CTR-decrypt using `IV[12..16] =
   htobe32(offset/16)`, pass bytes to Promise.
5. On `upload.cdnFileReuploadNeeded`: ping `upload.reuploadCdnFile` to
   the primary DC, then retry CDN.
6. On `FILE_REFERENCE_EXPIRED`: refresh via FileSourceManager, restart
   from Direct phase. Up to 3 retries.

Measured throughput on RTT-bound residential Israeli link:
- Serial: ~5 MiB/s
- 4 parallel workers: ~16-20 MiB/s (3.3× speedup)
- 8 parallel workers: regresses (TDLib/Telegram per-session inflight cap)

---

## Plugin schema fixes applied (also fyi for future maintenance)

These were TDLib API changes that accumulated in the years since the
plugin was last updated. Plugin was sending dropped/wrong fields and
TDLib's JSON parser silently ignored them, breaking features quietly:

| TDLib API | Old shape | New shape | Sites in plugin |
|---|---|---|---|
| `searchChatMessages` | `message_thread_id:int53` | `topic_id:MessageTopic` (object) | 6 |
| `sendMessage` | `reply_to_message_id:int53` (top-level) | `reply_to:InputMessageReplyTo` (nullable) | 3 (we dropped the dead `:0` field) |
| `sendMessage` | `disable_notification`/`from_background` at top level | wrap in `options:messageSendOptions` | 3 |
| `inputMessageText` (inside sendMessage) | `disable_web_page_preview:Bool` | `link_preview_options:linkPreviewOptions` (object) | 3 |
| `forwardMessages` | missing required fields | needs `message_thread_id`, `options` | 2 |
| `downloadFile` | missing required field | needs `synchronous:Bool` | 4 |
| `setAuthenticationPhoneNumber` | missing required field | needs structured `settings:phoneNumberAuthenticationSettings` | 2 |
| `getChats` | `offset_chat_id`, `offset_order` (separate ints) | `chat_list:ChatList` (object) | 8 |
| `searchMessages` | `offset_chat_id`, `offset_message_id` (separate) | `offset:string` (single opaque) | 1 |
| `searchPublicChats` | (no filter field) | requires `type_filter:SearchChatTypeFilter` | 2 |
| `authorizationStateWaitEncryptionKey` / `checkDatabaseEncryptionKey` | a separate auth step | removed entirely; use `database_encryption_key` in setTdlibParameters | 2 (dead handlers removed) |

If TDLib's CI starts rejecting unknown fields (currently it silently
ignores them), these silent drops would become loud errors. Probably
not going to happen, but worth knowing about.

---

## Deferred enhancements (not done, judged not worth the cost)

If streaming feels not-snappy enough in real use, these are the ranked
next steps:

1. **Cooperative cancellation on Kodi seek** — when a new GET arrives
   for the same file_id, the old GET's pool cancels its inflight
   chunks. ~30 lines in service.py. Saves wasted bandwidth on
   seek-heavy playback.
2. **HTTP keep-alive** in the local server (`protocol_version =
   "HTTP/1.1"`). 1 line. ~5-15 ms savings per Kodi range request.
3. **JSON loopback removal** — `default.py` HTTP POSTs to `service.py`
   on localhost to do TDLib calls; could be direct Python imports.
   ~40-line refactor. Measurable on menu navigation.
4. **`wait_response` future-based** — replace 10ms-polling loop with
   `threading.Event`. Saves ~5 ms median per call.
5. **Parallel mode-130 photo downloads** — ~5× faster on folder loads
   with many channels.

We did NOT do these — current performance is good enough for video
streaming. Revisit if needed.

---

## Other plugin files that need cleanup eventually

- The huge **dead triple-quoted block in default.py lines 401-413** —
  contains old `onPlayBackSeek`, `download_buffer`, etc. that look like
  live code in the IDE but are actually inside a `'''…'''` string.
  Booby trap. Delete or convert to real `#` comments next time you're
  in there.
- `client = 0` initialization at `service.py:73` should be `client =
  None`; calling `td_send` before login passes 0 (fake pointer) to
  the C API → access violation. We have a workaround (line 2087 is
  commented out) but the underlying bug is still there.
- The neutered `check_version()` function at top of service.py — has
  `return 0` at line 1, but if anyone removes that line during a
  refactor, the plugin downloads a remote DLL from `'Addr1'` URL and
  overwrites our custom build. Worth fully deleting.
- `'responce'` typo throughout service.py (should be `'response'`).
- Exception-logging boilerplate duplicated ~15-20× across both files;
  extract to a helper.

---

## Test harness for streaming

`C:\Users\ezrab\td\python_test\stream_test.py` runs a 7-test validation
suite against the built libtdjson.dll. Cached link in
`python_test/session/link_cache.json`.

Run against the production DLL:
```
cd C:\Users\ezrab\td\python_test
python stream_test.py <t.me/link>  --dll C:\Users\ezrab\td-pr\build-mingw64\libtdjson.dll
```

Or without args, interactive menu picks from the link cache.

Tests:
1. Cold start (time-to-first-byte)
2. Sequential throughput (20 MiB in 1 MiB chunks)
3. Seek TTFB (5 random positions)
4. Mid-stream seek (forward → jump → resume)
5. Byte-level consistency (same range twice, sha256 match)
6. Chunk-size sweep (64 KiB / 256 KiB / 1 MiB)
7. Parallelism sweep (1 / 2 / 4 / 8 workers)

---

## Quick reference: common commands

### Build production DLL+SO
```
C:\Users\ezrab\build-tdjson-production.bat
```

### Check what upstream's doing
```
cd C:\Users\ezrab\td-pr
git fetch upstream
git log upstream/master..production    # commits we have that upstream doesn't (should be 2)
git log production..upstream/master    # commits upstream has we don't (should be 0 after rebase)
```

### Verify deployed DLL is current
In Kodi: Settings → TDLIB version. Will show `1.8.65` (or whatever
upstream tip you last rebased to) + commit hash.

### Rollback DLL if a build breaks something
```
cd C:\Users\ezrab\plugin.video.telemedia\resources\lib\x64
copy /Y _backup_pre_readFileRemotePart\tdjson.dll tdjson.dll
```
Same pattern for Android `.so` files under `resources/lib/`.

### Rollback plugin Python code if a refactor breaks something
```
cd C:\Users\ezrab\plugin.video.telemedia
copy /Y service.py.bak.pre-readFileRemotePart service.py
```

### Force-rebuild from scratch (clear cmake state)
```
rmdir /s /q C:\Users\ezrab\td-pr\build-mingw64
mkdir C:\Users\ezrab\td-pr\build-mingw64
# Then re-run from MSYS2 MINGW64:
#   cd /c/Users/ezrab/td-pr/build-mingw64 && cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DTD_ENABLE_LTO=OFF ..
# Then build-tdjson-production.bat
```

---

## Background info you'd want anyway

- **Kodi shadows `min`** in some plugin contexts — that's why service.py
  uses `builtins.min` and `builtins.max` explicitly. Don't "simplify"
  those.
- **WSL `/mnt/c` is slow + breaks NDK symlinks** — that's why the Android
  build happens in `~/tdbuild/td/` (ext4) and only final `.so` files
  travel back over /mnt/c. Don't try to build Android directly from
  `/mnt/c/...`.
- **MinGW `libtdjson.dll` is bigger than MSVC's** (~31 MB stripped vs
  ~23 MB MSVC) because of libstdc++ template instantiation overhead.
  Expected and benign.
- **Stripping is automatic** in the production batch (`strip --strip-debug
  --strip-unneeded`). Android `build-tdlib.sh` strips its own .so files via
  `llvm-strip`.

---

## When you return: ~5-minute "is anything broken" check

1. Open Kodi (Windows or Android).
2. Settings → "TDLIB version" → confirm version string shows latest TDLib (e.g. `1.8.65`)
3. Try playing a video. It should start within ~1s and play smoothly.
4. Run the stream test:
   ```
   cd C:\Users\ezrab\td\python_test
   python stream_test.py
   ```
5. Glance at the PRs:
   - https://github.com/tdlib/td/pulls?q=is%3Apr+author%3AFalconEagleTest

If all green, nothing's broken. Otherwise the most likely culprits are:
- Stale plugin DLL (re-run `build-tdjson-production.bat`)
- Kodi cached an old Python module (close Kodi fully, restart)
- TDLib upstream pulled in a breaking schema change (re-run the
  schema-drift audit — see "Plugin schema fixes" table above)
