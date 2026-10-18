#!/usr/bin/env python3
"""Build the agentree zipapp: dist/agentree (stdlib only, uncompressed for fast imports).

Legacy-layout .pyc files (module.pyc next to module.py) are added by default: zipimport cannot
write a bytecode cache, so without them every start recompiles all modules (~30 ms). They are
compiled by the build interpreter; if the shebang python has a different bytecode magic,
zipimport ignores them and falls back to the .py sources.

  python3 scripts/build.py                 # -> dist/agentree, shebang = this interpreter
  python3 scripts/build.py --install       # also copy to ~/bin/agentree (mode 755)
  python3 scripts/build.py --python '/usr/bin/env python3'      # shebang other than this interpreter
"""
from __future__ import annotations

import argparse
import compileall
import os
import shutil
import sys
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PYTHON = sys.executable or "/usr/bin/env python3"   # agentree needs 3.14+


def build(out: str | None = None, python: str = DEFAULT_PYTHON, pyc: bool = True) -> str:
    out = out or os.path.join(ROOT, "dist", "agentree")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentree-build-") as stage:
        shutil.copytree(os.path.join(ROOT, "agentree"), os.path.join(stage, "agentree"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".*"))
        if pyc and not compileall.compile_dir(os.path.join(stage, "agentree"), legacy=True, quiet=1):
            raise SystemExit("byte-compilation failed")
        tmp = out + ".tmp"
        zipapp.create_archive(stage, tmp, interpreter=python, main="agentree.cli:main", compressed=False)
        os.chmod(tmp, 0o755)
        os.replace(tmp, out)
    return out


def install(src: str, dest: str | None = None) -> str:
    dest = dest or os.path.expanduser("~/bin/agentree")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--python", default=DEFAULT_PYTHON, help=f"shebang interpreter (default {DEFAULT_PYTHON})")
    p.add_argument("--out", help="output path (default dist/agentree)")
    p.add_argument("--no-pyc", action="store_true", help="ship sources only (no precompiled bytecode)")
    p.add_argument("--install", action="store_true", help="copy the result to ~/bin/agentree")
    a = p.parse_args(argv)
    out = build(a.out, a.python, pyc=not a.no_pyc)
    print(f"built {out} ({os.path.getsize(out)} bytes)")
    if a.install:
        print(f"installed {install(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
