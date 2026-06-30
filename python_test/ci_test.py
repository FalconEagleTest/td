"""CI smoke tests for libtdjson — no Telegram login required.

Tests:
  1. DLL loads without crashing
  2. Required symbols exist
  3. TDLib version matches CMakeLists.txt
  4. Custom patches are present in the schema

Usage:
  python ci_test.py --dll path/to/libtdjson.dll [--tl path/to/td_api.tl]
"""
import argparse
import ctypes
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_DLL = REPO / "build-mingw64" / "libtdjson.dll"
DEFAULT_TL  = REPO / "td" / "generate" / "scheme" / "td_api.tl"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results = []

def check(name: str, ok: bool, detail: str = ""):
    tag = PASS if ok else FAIL
    msg = f"[{tag}] {name}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    results.append((name, ok))
    return ok


def run_tests(dll_path: Path, tl_path: Path) -> int:
    print(f"\n=== TDLib CI smoke tests ===")
    print(f"DLL : {dll_path}")
    print(f"TL  : {tl_path}")
    print()

    # ── Test 1: DLL loads and client creates/destroys cleanly ──────────
    dll = None
    try:
        if os.name == "nt":
            os.add_dll_directory(str(dll_path.parent))
            # MinGW64 runtime DLLs live in msys64/mingw64/bin
            msys_bin = Path("C:/msys64/mingw64/bin")
            if msys_bin.exists():
                os.add_dll_directory(str(msys_bin))
        dll = ctypes.CDLL(str(dll_path))
        dll.td_json_client_create.restype  = ctypes.c_void_p
        dll.td_json_client_create.argtypes = []
        dll.td_json_client_destroy.restype  = None
        dll.td_json_client_destroy.argtypes = [ctypes.c_void_p]
        client = dll.td_json_client_create()
        assert client, "td_json_client_create returned NULL"
        dll.td_json_client_destroy(client)
        check("DLL loads + create/destroy", True,
              f"handle=0x{client:x}")
    except Exception as e:
        check("DLL loads + create/destroy", False, str(e))
        # Can't continue without a working DLL
        print("\nFatal: DLL unusable, skipping remaining tests.")
        return 1

    # ── Test 2: Required symbols present ──────────────────────────────
    required_symbols = [
        "td_json_client_create",
        "td_json_client_send",
        "td_json_client_receive",
        "td_json_client_destroy",
        "td_json_client_execute",
    ]
    missing = []
    for sym in required_symbols:
        try:
            getattr(dll, sym)
        except AttributeError:
            missing.append(sym)
    check("Required symbols present", not missing,
          f"missing: {missing}" if missing else f"{len(required_symbols)} symbols OK")

    # ── Test 3: TDLib version matches CMakeLists.txt ──────────────────
    try:
        cml = (REPO / "CMakeLists.txt").read_text(encoding="utf-8")
        m = re.search(r'project\(TDLib VERSION ([\d.]+)', cml)
        expected_version = m.group(1) if m else None

        dll.td_json_client_execute.restype  = ctypes.c_char_p
        dll.td_json_client_execute.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        raw = dll.td_json_client_execute(
            None,
            b'{"@type":"getOption","name":"version","@extra":1}')
        resp = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        actual_version = resp.get("value", "?")

        ok = (expected_version is None or actual_version == expected_version)
        check("TDLib version matches CMakeLists.txt", ok,
              f"binary={actual_version}  cmake={expected_version}")
    except Exception as e:
        check("TDLib version matches CMakeLists.txt", False, str(e))

    # ── Test 4: Custom patches present in schema ───────────────────────
    if tl_path.exists():
        tl = tl_path.read_text(encoding="utf-8")

        patches = {
            "readFileRemotePart in td_api.tl":
                "readFileRemotePart" in tl,
            "readFileRemotePart has correct signature (file_id offset count)":
                bool(re.search(
                    r"readFileRemotePart\s+file_id:int32\s+offset:int53\s+count:int32",
                    tl)),
        }
        for desc, ok in patches.items():
            check(desc, ok)
    else:
        check("Schema file found", False, f"not found: {tl_path}")

    # ── Summary ────────────────────────────────────────────────────────
    total  = len(results)
    passed = sum(1 for _, ok in results if ok)
    failed = total - passed
    print(f"\n{'='*40}")
    print(f"  {passed}/{total} passed"
          + (f"  — {failed} FAILED" if failed else "  — ALL PASS"))
    print(f"{'='*40}\n")

    return 0 if failed == 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    ap.add_argument("--tl",  type=Path, default=DEFAULT_TL)
    args = ap.parse_args()

    if not args.dll.exists():
        print(f"ERROR: DLL not found: {args.dll}", file=sys.stderr)
        sys.exit(1)

    sys.exit(run_tests(args.dll, args.tl))


if __name__ == "__main__":
    main()
