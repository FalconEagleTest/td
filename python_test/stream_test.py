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
import concurrent.futures
import ctypes
import hashlib
import json
import os
import queue
import random
import sys
import threading
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
TDLIB_LOG_FILE = SESSION_DIR / "tdlib.log"
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
    tdjson.td_json_client_execute.restype = ctypes.c_char_p
    tdjson.td_json_client_execute.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    tdjson.td_set_log_verbosity_level.restype = None
    tdjson.td_set_log_verbosity_level.argtypes = [ctypes.c_int]

    # Verbosity 2 = warnings. The C++ patch emits "[stream_diag]" lines
    # at WARNING so we can count CDN-vs-direct responses post-hoc.
    tdjson.td_set_log_verbosity_level(2)

    # Redirect log to a file so we can grep [stream_diag] later.
    # Truncate previous run's log.
    try:
        TDLIB_LOG_FILE.unlink()
    except FileNotFoundError:
        pass
    set_log = json.dumps({
        "@type": "setLogStream",
        "log_stream": {
            "@type": "logStreamFile",
            "path": str(TDLIB_LOG_FILE),
            "max_file_size": 50 * 1024 * 1024,
            "redirect_stderr": False,
        },
    }).encode("utf-8")
    tdjson.td_json_client_execute(None, set_log)
    return tdjson


_STREAM_DIAG_EVENTS = (
    "upload_file",          # direct DC response, no CDN
    "cdn_redirect",         # primary DC told us to fetch from CDN
    "upload_cdnFile",       # successful CDN fetch (decrypted)
    "cdn_reupload_needed",  # CDN said "reupload first"
    "cdn_reuploaded",       # primary DC confirmed reupload
    "cdn_sim_decrypt",      # TD_STREAM_FORCE_CDN_SIM=1 round-trip check
)


def count_stream_diag(log_path: Path = TDLIB_LOG_FILE) -> dict:
    """Tally [stream_diag] events from the TDLib log."""
    counts = {ev: 0 for ev in _STREAM_DIAG_EVENTS}
    if not log_path.exists():
        return {**counts, "total": 0, "log_missing": True}
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "[stream_diag]" not in line:
                continue
            for ev in _STREAM_DIAG_EVENTS:
                if f"] {ev} " in line:
                    counts[ev] += 1
                    break
    counts["total"] = sum(counts[ev] for ev in _STREAM_DIAG_EVENTS)
    return counts


def print_stream_diag(prev: dict | None = None) -> dict:
    """Print the current stream_diag counts; if `prev` is given, show
    the delta relative to it (so multiple runs share one log file)."""
    cur = count_stream_diag()
    if prev is None:
        delta = cur
    else:
        delta = {k: cur.get(k, 0) - prev.get(k, 0) for k in cur}
        delta["total"] = sum(delta[ev] for ev in _STREAM_DIAG_EVENTS)
    print("\n--- stream_diag (from TDLib log) ---")
    if delta.get("total", 0) == 0:
        print("  no chunks observed — log empty or missing")
        return cur
    direct = delta["upload_file"]
    cdn_first = delta["cdn_redirect"]
    cdn_data = delta["upload_cdnFile"]
    cdn_reup_need = delta["cdn_reupload_needed"]
    cdn_reup_done = delta["cdn_reuploaded"]
    cdn_sim = delta["cdn_sim_decrypt"]
    print(f"  direct (upload_file)            : {direct}")
    print(f"  CDN redirects observed          : {cdn_first}")
    print(f"  CDN bytes decrypted             : {cdn_data}")
    print(f"  CDN reupload required           : {cdn_reup_need}")
    print(f"  CDN reupload completed          : {cdn_reup_done}")
    if cdn_sim > 0:
        print(f"  cdn_sim_decrypt roundtrips      : {cdn_sim}")
    if cdn_data > 0 or cdn_first > 0:
        print(f"  -> CDN code path was exercised")
    elif cdn_sim > 0:
        print(f"  -> CDN math validated via "
              f"TD_STREAM_FORCE_CDN_SIM (no real CDN file hit)")
    else:
        print(f"  -> CDN code path NOT exercised (file is not CDN-cached)")
    return cur


class TDClient:
    """Thread-safe TDLib client.

    One background thread does `td_json_client_receive` and routes
    events to per-call Futures keyed by `@extra`. Lets `call()` be
    invoked from multiple threads concurrently — needed for the
    parallel-chunk throughput benchmark and for any client that wants
    to overlap requests with the receive loop.
    """
    def __init__(self, tdjson):
        self._td = tdjson
        self._client = tdjson.td_json_client_create()
        self._waiters: dict[int, concurrent.futures.Future] = {}
        self._waiter_lock = threading.Lock()
        self._auth_q: "queue.Queue[dict]" = queue.Queue()
        self._auth_mode = True  # while True, also tee events to _auth_q
        self._stop_recv = False
        self._recv_thread = threading.Thread(
            target=self._receive_loop, daemon=True, name="td-recv")
        self._recv_thread.start()

    def _receive_loop(self) -> None:
        while not self._stop_recv:
            try:
                raw = self._td.td_json_client_receive(self._client, 0.5)
            except Exception:
                continue
            if not raw:
                continue
            try:
                evt = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            extra = evt.get("@extra")
            delivered = False
            if extra is not None:
                with self._waiter_lock:
                    fut = self._waiters.pop(extra, None)
                if fut is not None and not fut.done():
                    fut.set_result(evt)
                    delivered = True
            # During the auth dance, also expose every event so the
            # state-machine driver can see updateAuthorizationState
            # and the various "error" events.
            if self._auth_mode and not delivered:
                self._auth_q.put(evt)

    def send(self, query: dict) -> None:
        self._td.td_json_client_send(
            self._client, json.dumps(query).encode("utf-8"))

    def receive(self, timeout: float = 1.0) -> dict | None:
        """Used only during the auth state-machine before _auth_mode flips."""
        try:
            return self._auth_q.get(timeout=timeout)
        except Exception:
            return None

    def end_auth_mode(self) -> None:
        """After login, stop teeing events into the auth queue."""
        self._auth_mode = False

    def call(self, query: dict, timeout: float = 30.0) -> dict:
        """Send a query, wait for the matching @extra response.

        Safe to call from multiple threads concurrently.
        """
        extra = random.randint(1, 2**31 - 1)
        query = dict(query, **{"@extra": extra})
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._waiter_lock:
            self._waiters[extra] = fut
        self.send(query)
        try:
            evt = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            with self._waiter_lock:
                self._waiters.pop(extra, None)
            raise TimeoutError(
                f"No response for {query['@type']} in {timeout}s")
        if evt.get("@type") == "error":
            raise RuntimeError(
                f"{query['@type']} failed: "
                f"{evt.get('code')} {evt.get('message')}")
        return evt

    def destroy(self):
        if self._client:
            self._stop_recv = True
            try:
                self._recv_thread.join(timeout=1.0)
            except Exception:
                pass
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
            # Stop teeing every event into the auth queue — from here on
            # only call() (via @extra futures) needs to see them.
            client.end_auth_mode()
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


def test_parallelism(client: TDClient, file_id: int, size: int) -> bool:
    """Compare sequential vs N-way parallel 1 MiB fetches.

    Tells us whether issuing concurrent readFileRemotePart calls gives
    real throughput gains — i.e., whether playback is RTT-bound (gains)
    or bandwidth-bound (flat). Drives the decision on whether to add
    pipelining in service.py's do_GET.
    """
    span_mb = 16
    print(f"\n[P] Parallelism sweep  ({span_mb} MiB across 1, 2, 4, 8 workers)")
    if size < (span_mb + 1) * MAX_CHUNK:
        print("    file too small — skipped")
        return True

    # Random aligned start; same start for each concurrency setting so
    # the comparison isn't biased by which region of the file is hot.
    random.seed(42)
    base = _aligned(random.randint(MAX_CHUNK, size - (span_mb + 1) * MAX_CHUNK),
                    MAX_CHUNK)
    offsets = [base + i * MAX_CHUNK for i in range(span_mb)]

    rows = []
    for workers in (1, 2, 4, 8):
        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda off: stream_chunk(client, file_id, off, MAX_CHUNK),
                offsets))
        elapsed = time.monotonic() - t0
        bytes_got = sum(len(r) for r in results)
        rate = bytes_got / 1024 / 1024 / elapsed if elapsed else 0
        rows.append((workers, elapsed, rate))
        print(f"    {workers:>2} workers  {bytes_got/1024/1024:5.1f} MiB in "
              f"{elapsed:5.2f}s  = {rate:5.2f} MiB/s")

    serial_rate = rows[0][2]
    best_workers, best_elapsed, best_rate = max(rows, key=lambda r: r[2])
    if serial_rate > 0:
        speedup = best_rate / serial_rate
        print(f"    best: {best_workers} workers, "
              f"{speedup:.2f}x vs serial")
    return rows[0][1] > 0  # PASS as long as serial run completed


def test_mid_stream_seek(client: TDClient, file_id: int, size: int) -> bool:
    """Stream 2 MiB, seek to 75%, stream 2 MiB more — covers the actor lifecycle."""
    print("\n[6] Mid-stream seek  (forward read -> jump -> forward read)")
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


def probe_cdn(client: TDClient, file_id: int) -> str:
    """One 1 MiB fetch at offset 0 to determine CDN routing.

    Returns 'cdn' / 'direct' / 'unknown' based on what the
    [stream_diag] log line said for this probe call.
    """
    pre = count_stream_diag()
    try:
        stream_chunk(client, file_id, 0, MAX_CHUNK)
    except Exception as e:
        return f"error ({e})"
    # Tiny delay to let the log file flush — TDLib's file log writes
    # are usually synchronous, but the diagnostic LOG line happens
    # immediately before promise.set_value, so a few ms of slack is safe.
    time.sleep(0.05)
    cur = count_stream_diag()
    if cur["cdn_sim_decrypt"] > pre["cdn_sim_decrypt"]:
        return "cdn_sim"
    cdn_hit = (cur["cdn_redirect"] > pre["cdn_redirect"]
               or cur["upload_cdnFile"] > pre["upload_cdnFile"])
    direct_hit = cur["upload_file"] > pre["upload_file"]
    if cdn_hit:
        return "cdn"
    if direct_hit:
        return "direct"
    return "unknown"


def run_validation_suite(client: TDClient, rec: dict) -> None:
    print(f"\n=== Streaming validation: {rec['name']} "
          f"({rec['size']/1024/1024:.2f} MiB) ===")

    # Fast precheck: one chunk so the user can see the routing verdict
    # before committing to the full ~30-60s suite.
    print("\n[probe] checking CDN routing with one 1 MiB fetch...")
    pre_probe = count_stream_diag()
    routing = probe_cdn(client, rec["file_id"])
    if routing == "cdn":
        print("[probe] -> CDN-routed. CDN decrypt + reupload code WILL "
              "be exercised by the suite.")
    elif routing == "cdn_sim":
        print("[probe] -> CDN simulation (TD_STREAM_FORCE_CDN_SIM=1). "
              "Every chunk will round-trip through the AES-CTR code.")
    elif routing == "direct":
        print("[probe] -> direct from primary DC. CDN code path NOT "
              "exercised (file isn't CDN-cached).")
        print("[probe]    Ctrl-C in the next 3 seconds to skip this "
              "file.")
        try:
            time.sleep(3.0)
        except KeyboardInterrupt:
            print("\n[probe] aborted by user.")
            return
    else:
        print(f"[probe] -> {routing}")

    tests = [
        test_cold_start,
        test_sequential,
        test_seek_ttfb,
        test_mid_stream_seek,
        test_consistency,
        test_chunk_sweep,
        test_parallelism,
    ]
    pre_counts = pre_probe  # the probe itself is included in this run's delta
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
    print_stream_diag(prev=pre_counts)


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
