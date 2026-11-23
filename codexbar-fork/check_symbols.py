#!/usr/bin/env python3
"""Checks the built CodexBar.app: are all the system functions it references present in this macOS.

Why: Xcode can be newer than the installed system (for example, a final Xcode on a macOS beta). Then the app
references functions that the system does not have yet and crashes at launch without a clear error. This happened
on 23.11.2026 with `Date >=` and `Date <=` (a shim was added for them in Sources/LemelsonCompat).

Usage: check_symbols.py /path/to/CodexBar.app   -> exit code 0 if everything is present, otherwise a list of missing functions.
"""
import ctypes
import glob
import os
import re
import subprocess
import sys


def binaries(app):
    found = []
    for pattern in ("Contents/MacOS/*", "Contents/Helpers/*", "Contents/PlugIns/*/Contents/MacOS/*"):
        for path in glob.glob(os.path.join(app, pattern)):
            if os.path.isfile(path) and open(path, "rb").read(4) in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
                found.append(path)
    return found


def missing(path):
    for line in subprocess.run(["otool", "-L", path], capture_output=True, text=True).stdout.splitlines()[1:]:
        lib = line.strip().split(" (")[0]
        if lib.startswith("/"):
            try:
                ctypes.CDLL(lib)
            except OSError:
                pass
    here = ctypes.CDLL(None)
    out = []
    listing = subprocess.run(["nm", "-m", path], capture_output=True, text=True).stdout
    for name, lib in re.findall(r"weak external (\S+) \(from ([^)]+)\)", listing):
        try:
            getattr(here, name[1:] if name.startswith("_") else name)
        except AttributeError:
            out.append((name, lib))
    return out


def main():
    app = sys.argv[1]
    bad = 0
    for path in binaries(app):
        gone = missing(path)
        if not gone:
            continue
        bad += len(gone)
        names = subprocess.run(["xcrun", "swift-demangle", "--simplified"] + [g[0] for g in gone],
                               capture_output=True, text=True).stdout.splitlines()
        print(f"{os.path.relpath(path, app)}: this macOS lacks {len(gone)} functions that the build references:")
        for (name, lib), pretty in zip(gone, names):
            print(f"  {lib}: {pretty.split(' ---> ')[-1]}")
    if bad:
        print("The build will crash at launch. Cause: Xcode is newer than the installed macOS. Update macOS "
              "or add a shim following the example of Sources/LemelsonCompat.")
        return 1
    print(f"System functions are present ({len(binaries(app))} files checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
