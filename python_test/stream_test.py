"""End-to-end test for the readFileRemotePart streaming API.

Run it once interactively to log in; the auth survives in ./session/.
Subsequent runs accept a Telegram message link (or a cached one) and
stream the file with readFileRemotePart, writing it to ./downloads/.

Usage:
    python stream_test.py                 # menu (login, list cache, stream)
    python stream_test.py <message_link>  # stream a specific link
    python stream_test.py --bench <link>  # stream + throughput report
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

API_ID = 50322
API_HASH = "9ff1a639196c0779c86dd661af8522ba"

HERE = Path(__file__).resolve().parent
TD_REPO = HERE.parent
DEFAULT_DLL = TD_REPO / "build-mingw64" / "libtdjson.dll"

SESSION_DIR = HERE / "session"
DOWNLOAD_DIR = HERE / "downloads"
CACHE_FILE = SESSION_DIR / "link_cache.json"
SESSION_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)


def load_tdjson(dll_path: Path):
    if not dll_path.exists():
        sys.exit(f"libtdjson.dll not found at {dll_path}. "
                 "Build TDLib first or pass --dll.")
    # Add the DLL's own directory first (bundled OpenSSL/zlib live there).
    # Only fall back to MSYS2's mingw64/bin if the DLL itself is the
    # MinGW build we produced — otherwise mixing OpenSSL ABIs would
    # silently load the wrong libcrypto/libssl alongside the DLL.
    if os.name == "nt":
        os.add_dll_directory(str(dll_path.parent))
        if "mingw64" in str(dll_path).lower().replace("\\", "/"):
            msys_bin = Path("C:/msys64/mingw64/bin")
            if msys_bin.exists():
                os.add_dll_directory(str(msys_bin))
    tdjson = ctypes.CDLL(str(dll_path))
    tdjson.td_json_client_create.restype = ctypes.c_void_p
    tdjson.td_json_client_create.argtypes = []
    tdjson.td_json_client_send.restype = None
    tdjson.td_json_client_send.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    tdjson.td_json_client_receive.restype = ctypes.c_char_p
    tdjson.td_json_client_receive.argtypes = [ctypes.c_void_p, ctypes.c_double]
    tdjson.td_json_client_destroy.restype = None
    tdjson.td_json_client_destroy.argtypes = [ctypes.c_void_p]
    tdjson.td_set_log_verbosity_level.restype = None
    tdjson.td_set_log_verbosity_level.argtypes = [ctypes.c_int]
    tdjson.td_set_log_verbosity_level(1)
    return tdjson


class TDClient:
    def __init__(self, tdjson):
        self._td = tdjson
        self._client = tdjson.td_json_client_create()

    def send(self, query: dict) -> None:
        self._td.td_json_client_send(
            self._client, json.dumps(query).encode("utf-8"))

    def receive(self, timeout: float = 1.0) -> dict | None:
        raw = self._td.td_json_client_receive(self._client, timeout)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def call(self, query: dict, timeout: float = 30.0) -> dict:
        """Send a query and wait for the matching @extra response."""
        extra = random.randint(1, 2**31 - 1)
        query = dict(query, **{"@extra": extra})
        self.send(query)
        deadline = time.time() + timeout
        while time.time() < deadline:
            evt = self.receive(0.5)
            if not evt:
                continue
            if evt.get("@extra") == extra:
                if evt.get("@type") == "error":
                    raise RuntimeError(
                        f"{query['@type']} failed: "
                        f"{evt.get('code')} {evt.get('message')}")
                return evt
        raise TimeoutError(f"No response for {query['@type']} in {timeout}s")

    def destroy(self):
        if self._client:
            self._td.td_json_client_destroy(self._client)
            self._client = None


# ---------------------------- Authorization -------------------------------

_CODE_TYPE_HINT = {
    "authenticationCodeTypeTelegramMessage":
        "sent to your Telegram app on another logged-in device",
    "authenticationCodeTypeSms": "sent by SMS",
    "authenticationCodeTypeCall": "delivered by an automated voice call",
    "authenticationCodeTypeFlashCall":
        "delivered as a flash call — last digits of the caller number ARE the code",
    "authenticationCodeTypeMissedCall":
        "delivered as a missed call — last digits of caller number ARE the code",
    "authenticationCodeTypeFragment": "sent to your Fragment account",
    "authenticationCodeTypeFirebaseAndroid": "via Firebase (Android)",
    "authenticationCodeTypeFirebaseIos": "via Firebase (iOS)",
}


def _describe_code_info(auth_state: dict) -> str:
    info = auth_state.get("code_info") or {}
    t = (info.get("type") or {}).get("@type", "unknown")
    msg = _CODE_TYPE_HINT.get(t, f"type={t}")
    phone = info.get("phone_number")
    if phone:
        msg = f"to phone +{phone} — " + msg
    nxt = info.get("next_type")
    if nxt:
        nt = nxt.get("@type", "")
        nice = _CODE_TYPE_HINT.get(nt, nt)
        msg += f" (next attempt: {nice})"
    timeout = info.get("timeout")
    if timeout:
        msg += f" — resend available in {timeout}s"
    return msg


def authorize(client: TDClient) -> None:
    """Drive the auth state machine until authorizationStateReady."""
    client.send({"@type": "getAuthorizationState"})
    while True:
        evt = client.receive(1.0)
        if not evt:
            continue
        # Errors from auth requests (bad phone, wrong code, etc.) come as
        # standalone "error" events, not as a new authorization state.
        # Surface them or the user just sees the prompt hang.
        if evt.get("@type") == "error":
            print(f"  TDLib error: {evt.get('code')} {evt.get('message')}")
            # Re-query auth state so we re-prompt for whatever it wants.
            client.send({"@type": "getAuthorizationState"})
            continue
        if evt.get("@type") != "updateAuthorizationState":
            continue
        state = evt["authorization_state"]["@type"]

        if state == "authorizationStateWaitTdlibParameters":
            # Match service.py exactly — Telegram's delivery heuristics
            # use device_model/system_version/application_version, and an
            # unfamiliar-looking client can get the in-app code message
            # silently filtered on the receiving Telegram app.
            client.send({
                "@type": "setTdlibParameters",
                "database_directory": str(SESSION_DIR / "tdlib_db"),
                "files_directory": str(SESSION_DIR / "tdlib_files"),
                "use_message_database": True,
                "use_secret_chats": True,
                "api_id": API_ID,
                "api_hash": API_HASH,
                "system_language_code": "en",
                "device_model": "Desktop",
                "system_version": "Linux",
                "application_version": "1.0",
                "enable_storage_optimizer": True,
            })
        elif state == "authorizationStateWaitPhoneNumber":
            print("Login method:")
            print("  [1] Phone number + SMS/Telegram code")
            print("  [2] QR code (scan from your already-logged-in Telegram app)")
            choice = input("> ").strip()
            if choice == "2":
                client.send({"@type": "requestQrCodeAuthentication"})
            else:
                raw = input(
                    "Phone number (with country code, no +): ").strip()
                # Tolerant cleanup: strip whitespace, dashes, parens, leading +.
                phone = "".join(c for c in raw if c.isdigit())
                if not phone:
                    print("  (empty / no digits — try again)")
                    continue
                print(f"  registering phone +{phone}")
                client.send({
                    "@type": "setAuthenticationPhoneNumber",
                    "phone_number": phone,
                })
        elif state == "authorizationStateWaitOtherDeviceConfirmation":
            link = evt["authorization_state"]["link"]
            print()
            print(f"  QR link: {link}")
            print()
            try:
                import qrcode  # type: ignore
                q = qrcode.QRCode(border=1)
                q.add_data(link)
                q.make(fit=True)
                q.print_ascii(invert=True)
            except ImportError:
                print("  (install `pip install qrcode` for an inline QR)")
            print("To authorize, open Telegram on a logged-in device →")
            print("  Settings → Devices → Link Desktop Device → scan the QR.")
            print("Waiting for confirmation...")
        elif state == "authorizationStateWaitCode":
            print(f"  code {_describe_code_info(evt['authorization_state'])}")
            code = input("Login code: ").strip()
            if not code:
                # empty input → ask TDLib to try the next delivery channel
                # (typically: Telegram message → SMS).
                print("  requesting resend via next channel...")
                client.send({"@type": "resendAuthenticationCode"})
                continue
            client.send({
                "@type": "checkAuthenticationCode",
                "code": code,
            })
        elif state == "authorizationStateWaitPassword":
            import getpass
            pw = getpass.getpass("2FA password: ")
            client.send({
                "@type": "checkAuthenticationPassword",
                "password": pw,
            })
        elif state == "authorizationStateWaitRegistration":
            sys.exit("This account isn't registered. Register via an "
                     "official Telegram app first.")
        elif state == "authorizationStateReady":
            print("Logged in.")
            return
        elif state == "authorizationStateClosed":
            sys.exit("Authorization closed unexpectedly.")
        # other transient states (WaitEncryptionKey on older TDLib, etc.)
        # are not used in 1.8+ but we just ignore them.


# ---------------------------- Cache --------------------------------------

def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _extract_file(content: dict) -> tuple[int, int, str] | None:
    """Return (file_id, size, display_name) for any media-bearing message."""
    t = content.get("@type", "")
    if t == "messageVideo":
        v = content["video"]
        return (v["video"]["id"], v["video"]["size"],
                v.get("file_name") or "video.mp4")
    if t == "messageDocument":
        d = content["document"]
        return (d["document"]["id"], d["document"]["size"],
                d.get("file_name") or "document.bin")
    if t == "messageAnimation":
        a = content["animation"]
        return (a["animation"]["id"], a["animation"]["size"],
                a.get("file_name") or "animation.mp4")
    if t == "messageAudio":
        au = content["audio"]
        return (au["audio"]["id"], au["audio"]["size"],
                au.get("file_name") or "audio.mp3")
    return None


def _refresh_file_id(client: TDClient, chat_id: int,
                     message_id: int) -> tuple[int, int, str]:
    """Refetch a message and return its current (file_id, size, name).

    file_id is session-local — it changes every TDLib restart. chat_id
    and message_id are stable, so we use them as the durable handle.
    """
    msg = client.call({
        "@type": "getMessage",
        "chat_id": chat_id,
        "message_id": message_id,
    })
    extracted = _extract_file(msg.get("content", {}))
    if not extracted:
        raise RuntimeError(
            f"Message {chat_id}/{message_id} has no streamable file")
    return extracted


def resolve_link(client: TDClient, link: str, cache: dict) -> dict:
    """Return {file_id, size, name, chat_id, message_id} for a t.me link.

    chat_id and message_id are cached durably; file_id is refreshed
    every session because TDLib reassigns it on restart.
    """
    if link in cache:
        rec = cache[link]
        try:
            file_id, size, name = _refresh_file_id(
                client, rec["chat_id"], rec["message_id"])
            rec.update(file_id=file_id, size=size, name=name)
            cache[link] = rec
            save_cache(cache)
            print(f"cache hit (refreshed file_id={file_id}): {name}")
            return rec
        except RuntimeError as e:
            print(f"  cached entry stale ({e}); re-resolving link")

    info = client.call({"@type": "getMessageLinkInfo", "url": link})
    msg = info.get("message")
    if not msg:
        raise RuntimeError(f"No message at link: {link}")
    extracted = _extract_file(msg.get("content", {}))
    if not extracted:
        raise RuntimeError(
            f"Message at {link} has no streamable file "
            f"(content type {msg.get('content', {}).get('@type')})")
    file_id, size, name = extracted

    record = {
        "file_id": file_id,
        "size": size,
        "name": name,
        "chat_id": msg["chat_id"],
        "message_id": msg["id"],
        "resolved_at": int(time.time()),
    }
    cache[link] = record
    save_cache(cache)
    print(f"resolved: {name} ({size:,} bytes, file_id={file_id})")
    return record


# ---------------------------- Streaming ----------------------------------

# MTProto upload.getFile rules — see download_stream_part in FileManager.cpp.
ALIGN = 4096
MAX_CHUNK = 1024 * 1024  # 1 MiB
VALID_CHUNKS = [c for c in (
    4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576)]


def stream_chunk(client: TDClient, file_id: int, offset: int,
                 count: int, no_store: bool = True) -> bytes:
    """One readFileRemotePart call. Returns the raw bytes from the server."""
    try:
        resp = client.call({
            "@type": "readFileRemotePart",
            "file_id": file_id,
            "offset": offset,
            "count": count,
            "no_store": no_store,
        })
    except RuntimeError as e:
        # Pin the offset/count to the error so we can see the misalignment.
        raise RuntimeError(
            f"readFileRemotePart(file_id={file_id}, "
            f"offset={offset}, count={count}): {e}") from None
    if resp.get("@type") != "data":
        raise RuntimeError(f"Unexpected response: {resp}")
    return base64.b64decode(resp["data"])


def stream_file(client: TDClient, file_id: int, total_size: int,
                out_path: Path, chunk: int = MAX_CHUNK,
                bench: bool = False) -> Path:
    """Stream a whole file with readFileRemotePart into out_path.

    Invariant: `offset` is always 4 KiB-aligned. Each iteration requests
    a valid (offset, count) per MTProto rules. We pre-allocate the output
    file so we can seek+write each chunk at its true position — that way
    a short response from the server doesn't desync the next request.
    """
    if chunk not in VALID_CHUNKS:
        raise ValueError(f"chunk must be one of {VALID_CHUNKS}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    h = hashlib.sha256()
    bytes_written = 0
    start = time.monotonic()
    last_log = start

    with open(out_path, "wb") as f:
        # Pre-size the file so f.seek(offset)+write() lands at the right spot
        # even if chunks arrive out of order or partial.
        f.truncate(total_size)
        offset = 0
        while offset < total_size:
            remaining = total_size - offset
            # Pick the largest valid chunk that fits in the remainder.
            if remaining >= chunk:
                this_chunk = chunk
            else:
                smaller = [c for c in VALID_CHUNKS if c <= remaining]
                # If remainder < 4 KiB, still request 4 KiB and trim what
                # comes back; the server only returns the real bytes.
                this_chunk = smaller[-1] if smaller else ALIGN

            t0 = time.monotonic()
            data = stream_chunk(client, file_id, offset, this_chunk)
            t1 = time.monotonic()
            if not data:
                print(f"  EOF at offset {offset:,} (expected {total_size:,})")
                break

            # Trim padding past EOF.
            expected = remaining if remaining < this_chunk else this_chunk
            if len(data) > expected:
                data = data[:expected]

            f.seek(offset)
            f.write(data)
            h.update(data)
            bytes_written += len(data)

            # Advance by the REQUESTED chunk size to stay 4 KiB-aligned.
            # If the server returned short mid-file, we'll have a hole
            # in the output; in practice that only happens at EOF, which
            # the `not data` / `offset >= total_size` guards catch.
            offset += this_chunk

            if bench and (time.monotonic() - last_log) > 1.0:
                mb = bytes_written / 1024 / 1024
                elapsed = time.monotonic() - start
                rate = mb / elapsed if elapsed else 0
                pct = 100.0 * offset / total_size
                print(f"  {pct:5.1f}%  {mb:7.2f} MiB  "
                      f"{rate:6.2f} MiB/s  last chunk {t1-t0:.3f}s")
                last_log = time.monotonic()

    elapsed = time.monotonic() - start
    rate = bytes_written / 1024 / 1024 / elapsed if elapsed else 0
    print(f"streamed {bytes_written:,} bytes in {elapsed:.2f}s "
          f"({rate:.2f} MiB/s)")
    print(f"sha256: {h.hexdigest()}")
    return out_path


# ---------------------------- Validation suite ---------------------------

def _aligned(offset: int, boundary: int = ALIGN) -> int:
    """Round `offset` down to `boundary`."""
    return (offset // boundary) * boundary


def _stat(label: str, samples: list[float]) -> str:
    if not samples:
        return f"{label}: (no samples)"
    s = sorted(samples)
    n = len(s)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return (f"{label}: min {s[0]*1000:6.0f}ms  "
            f"median {median*1000:6.0f}ms  "
            f"max {s[-1]*1000:6.0f}ms")


def test_cold_start(client: TDClient, file_id: int, _size: int) -> bool:
    """Time-to-first-byte from offset 0 — what 'press play' feels like."""
    print("\n[1] Cold start  (offset=0, count=1 MiB)")
    t0 = time.monotonic()
    data = stream_chunk(client, file_id, 0, MAX_CHUNK)
    dt = time.monotonic() - t0
    ok = len(data) == MAX_CHUNK
    print(f"    first chunk in {dt*1000:6.0f}ms, "
          f"{len(data):,} bytes  {'OK' if ok else 'SHORT'}")
    return ok


def test_sequential(client: TDClient, file_id: int, size: int,
                    bytes_to_read: int = 20 * 1024 * 1024) -> bool:
    """Sequential throughput — the buffer-ahead path of a video player."""
    target = min(bytes_to_read, size)
    print(f"\n[2] Sequential throughput  ({target/1024/1024:.0f} MiB "
          "in 1 MiB chunks)")
    latencies = []
    bytes_got = 0
    offset = 0
    start = time.monotonic()
    while bytes_got < target and offset < size:
        remaining = target - bytes_got
        valid = [c for c in VALID_CHUNKS if c <= min(MAX_CHUNK, remaining)]
        this_chunk = valid[-1] if valid else ALIGN
        t0 = time.monotonic()
        data = stream_chunk(client, file_id, offset, this_chunk)
        latencies.append(time.monotonic() - t0)
        if not data:
            break
        bytes_got += len(data)
        offset += this_chunk
    elapsed = time.monotonic() - start
    rate = bytes_got / 1024 / 1024 / elapsed if elapsed else 0
    print(f"    {bytes_got/1024/1024:.2f} MiB in {elapsed:.2f}s "
          f"= {rate:.2f} MiB/s")
    print(f"    {_stat('chunk latency', latencies)}")
    return bytes_got >= target * 0.99


def test_seek_ttfb(client: TDClient, file_id: int, size: int,
                   n_seeks: int = 5) -> bool:
    """Time-to-first-byte at random positions — fast seek responsiveness."""
    print(f"\n[3] Seek TTFB  ({n_seeks} random positions, 1 MiB each)")
    if size < 2 * MAX_CHUNK:
        print("    file too small to seek meaningfully — skipped")
        return True
    random.seed(0)  # deterministic across runs
    # 1 MiB-align so [pos, pos+1MiB) lies in a single 1 MiB MTProto block.
    positions = sorted(
        _aligned(random.randint(0, size - MAX_CHUNK - 1), MAX_CHUNK)
        for _ in range(n_seeks))
    latencies = []
    for pos in positions:
        t0 = time.monotonic()
        data = stream_chunk(client, file_id, pos, MAX_CHUNK)
        dt = time.monotonic() - t0
        latencies.append(dt)
        pct = 100.0 * pos / size
        mark = "OK" if len(data) == MAX_CHUNK else f"SHORT({len(data)})"
        print(f"    seek to {pct:5.1f}% (offset {pos:>12,})  "
              f"TTFB {dt*1000:5.0f}ms  {mark}")
    print(f"    {_stat('seek TTFB', latencies)}")
    return all(t < 5.0 for t in latencies)


def test_consistency(client: TDClient, file_id: int, size: int) -> bool:
    """Same offset twice → identical bytes. Confirms no caching weirdness."""
    print("\n[4] Consistency  (read same 1 MiB twice from mid-file)")
    if size < 2 * MAX_CHUNK:
        print("    file too small — skipped")
        return True
    offset = _aligned(size // 2, MAX_CHUNK)
    a = stream_chunk(client, file_id, offset, MAX_CHUNK)
    b = stream_chunk(client, file_id, offset, MAX_CHUNK)
    ha = hashlib.sha256(a).hexdigest()
    hb = hashlib.sha256(b).hexdigest()
    ok = a == b and len(a) == MAX_CHUNK
    print(f"    read 1  sha256={ha[:16]}...  ({len(a):,} bytes)")
    print(f"    read 2  sha256={hb[:16]}...  ({len(b):,} bytes)")
    print(f"    identical: {ok}")
    return ok


def test_chunk_sweep(client: TDClient, file_id: int, size: int) -> bool:
    """How throughput varies with chunk size — guides chunk_size_new22 setting."""
    print("\n[5] Chunk-size sweep  (4 MiB per size, from mid-file)")
    if size < 8 * 1024 * 1024:
        print("    file too small — skipped")
        return True
    # 1 MiB-align the base so every sub-chunk in the span stays within
    # the same 1 MiB block sequence (avoids cross-boundary errors for
    # smaller chunk sizes that don't divide into the block neatly).
    base = _aligned(size // 2, MAX_CHUNK)
    span = 4 * 1024 * 1024
    rows = []
    for c in (65536, 262144, 1048576):
        calls = span // c
        t0 = time.monotonic()
        bytes_got = 0
        for i in range(calls):
            data = stream_chunk(client, file_id, base + i * c, c)
            bytes_got += len(data)
        elapsed = time.monotonic() - t0
        rate = bytes_got / 1024 / 1024 / elapsed if elapsed else 0
        rows.append((c, calls, bytes_got, elapsed, rate))
        print(f"    {c//1024:4} KiB chunks: {calls:3} calls  "
              f"{bytes_got/1024/1024:5.2f} MiB in {elapsed:5.2f}s  "
              f"= {rate:5.2f} MiB/s")
    # OK if at least one configuration delivers >0.5 MiB/s
    return any(r[4] > 0.5 for r in rows)


def test_mid_stream_seek(client: TDClient, file_id: int, size: int) -> bool:
    """Stream 2 MiB, seek to 75%, stream 2 MiB more — covers the actor lifecycle."""
    print("\n[6] Mid-stream seek  (forward read → jump → forward read)")
    if size < 16 * 1024 * 1024:
        print("    file too small — skipped")
        return True
    # Part A: 2 MiB from offset 0
    for i in range(2):
        d = stream_chunk(client, file_id, i * MAX_CHUNK, MAX_CHUNK)
        if len(d) != MAX_CHUNK:
            print(f"    part A short at {i}  FAIL")
            return False
    # Part B: jump to 75% and read 2 MiB
    seek = _aligned(int(size * 0.75), MAX_CHUNK)
    for i in range(2):
        t0 = time.monotonic()
        d = stream_chunk(client, file_id, seek + i * MAX_CHUNK, MAX_CHUNK)
        dt = time.monotonic() - t0
        if len(d) != MAX_CHUNK:
            print(f"    part B short at {i}  FAIL ({len(d)} bytes)")
            return False
        print(f"    seek+{i} TTFB {dt*1000:5.0f}ms  OK")
    return True


def run_validation_suite(client: TDClient, rec: dict) -> None:
    print(f"\n=== Streaming validation: {rec['name']} "
          f"({rec['size']/1024/1024:.2f} MiB) ===")
    tests = [
        test_cold_start,
        test_sequential,
        test_seek_ttfb,
        test_mid_stream_seek,
        test_consistency,
        test_chunk_sweep,
    ]
    results = []
    overall_start = time.monotonic()
    for t in tests:
        try:
            ok = t(client, rec["file_id"], rec["size"])
        except Exception as e:
            print(f"    EXCEPTION: {e}")
            ok = False
        results.append((t.__name__, ok))
    elapsed = time.monotonic() - overall_start
    print(f"\n--- summary ({elapsed:.1f}s) ---")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    overall = all(ok for _, ok in results)
    print(f"\n{'ALL PASS' if overall else 'SOME FAILED'}")


# ---------------------------- Main ---------------------------------------


def menu(client: TDClient, cache: dict) -> None:
    while True:
        print("\n--- stream_test ---")
        print("1) Validate a new link")
        if cache:
            print("2) Validate from cache:")
            for i, (link, rec) in enumerate(cache.items()):
                print(f"     [{i}] {rec['name']:30}  "
                      f"{rec['size']/1024/1024:7.2f} MiB   {link}")
            print("3) Full download from cache (writes to ./downloads/)")
        print("q) Quit")
        choice = input("> ").strip()
        if choice == "q":
            return
        if choice == "1":
            link = input("Telegram message link: ").strip()
            if not link:
                continue
            rec = resolve_link(client, link, cache)
            run_validation_suite(client, rec)
        elif choice == "2" and cache:
            idx = int(input("index: "))
            link, _ = list(cache.items())[idx]
            rec = resolve_link(client, link, cache)
            run_validation_suite(client, rec)
        elif choice == "3" and cache:
            idx = int(input("index: "))
            link, _ = list(cache.items())[idx]
            rec = resolve_link(client, link, cache)
            out = DOWNLOAD_DIR / rec["name"]
            stream_file(client, rec["file_id"], rec["size"], out, bench=True)
            print(f"saved -> {out}")
        else:
            continue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("link", nargs="?", help="Telegram message link to stream")
    ap.add_argument("--dll", type=Path, default=DEFAULT_DLL,
                    help=f"path to libtdjson.dll (default: {DEFAULT_DLL})")
    ap.add_argument("--bench", action="store_true",
                    help="print per-second throughput while streaming")
    ap.add_argument("--chunk", type=int, default=MAX_CHUNK,
                    choices=VALID_CHUNKS,
                    help="chunk size in bytes (default: 1 MiB)")
    ap.add_argument("--validate", action="store_true",
                    help="run the streaming validation suite "
                         "instead of a full download")
    ap.add_argument("--full", action="store_true",
                    help="download the whole file (default with positional "
                         "link if neither --validate nor --full is given "
                         "is --validate)")
    args = ap.parse_args()

    tdjson = load_tdjson(args.dll)
    client = TDClient(tdjson)
    try:
        authorize(client)
        cache = load_cache()
        if args.link:
            rec = resolve_link(client, args.link, cache)
            if args.full:
                out = DOWNLOAD_DIR / rec["name"]
                stream_file(client, rec["file_id"], rec["size"], out,
                            chunk=args.chunk, bench=args.bench)
                print(f"saved -> {out}")
            else:
                # default with positional link: validation suite
                run_validation_suite(client, rec)
        else:
            menu(client, cache)
    finally:
        client.destroy()


if __name__ == "__main__":
    main()
