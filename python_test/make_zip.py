"""Bundle all files in a directory into a zip archive.

Usage: python make_zip.py <source_dir> <output.zip>
"""
import sys
import zipfile
from pathlib import Path

src = Path(sys.argv[1])
out = Path(sys.argv[2])

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(src.iterdir()):
        if f.is_file():
            z.write(f, f.name)
            print(f"  added {f.name} ({f.stat().st_size // 1024}K)")

print(f"Created {out} ({out.stat().st_size // 1024}K total)")
