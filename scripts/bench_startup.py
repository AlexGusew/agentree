#!/usr/bin/env python3
"""Startup bench: spawn dist/agentree in a pty and time spawn -> first painted frame.

The child gets AGENTREE_BENCH_FIRST_FRAME=<file>; cli passes run_app an on_first_frame hook that
writes "<unix time> <in-process ms since cli import>" there and exits. AGENTREE_STATE_DIR points at
a temp dir holding a cached sample snapshot, so nothing is gathered and real state is untouched.

  python3 scripts/bench_startup.py [--runs 10] [--app dist/agentree] [--python /usr/bin/python3.14]
"""
from __future__ import annotations

import argparse
import fcntl
import os
import select
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_MS = 150.0


def _seed_state(state_dir: str) -> None:
    code = ("import time; from agentree import state; from agentree.model.sample import sample_snapshot; "
            "state.save_cache(sample_snapshot(time.time()))")
    env = dict(os.environ, AGENTREE_STATE_DIR=state_dir, PYTHONPATH=ROOT)
    subprocess.run([sys.executable, "-c", code], env=env, check=True)


def run_once(cmd: list[str], env: dict[str, str], marker: str, timeout: float = 10.0) -> tuple[float, float]:
    if os.path.exists(marker):
        os.unlink(marker)
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
    t_spawn = time.time()
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True,
                            close_fds=True)
    os.close(slave)
    out = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None and time.monotonic() < deadline:
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                try:
                    out += os.read(master, 65536)   # keep the pty drained
                except OSError:
                    break
        if proc.poll() is None:
            proc.kill()
            raise RuntimeError(f"timed out; output tail: {bytes(out[-400:])!r}")
    finally:
        proc.wait()
        os.close(master)
    try:
        with open(marker) as fh:
            t_frame, inproc_ms = (float(x) for x in fh.read().split())
    except (OSError, ValueError):
        raise RuntimeError(f"no first-frame marker (exit {proc.returncode}); output tail: {bytes(out[-400:])!r}")
    return (t_frame - t_spawn) * 1000, inproc_ms


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--app", default=os.path.join(ROOT, "dist", "agentree"))
    p.add_argument("--python", help="run the app with this interpreter instead of its shebang")
    a = p.parse_args(argv)
    if not os.path.exists(a.app):
        print(f"{a.app} missing: run scripts/build.py first", file=sys.stderr)
        return 1
    cmd = ([a.python] if a.python else []) + [a.app]
    with tempfile.TemporaryDirectory(prefix="agentree-bench-") as tmp:
        state_dir = os.path.join(tmp, "state")
        _seed_state(state_dir)
        marker = os.path.join(tmp, "first-frame")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTREE_", "PYTHON"))}
        env.update(AGENTREE_STATE_DIR=state_dir, AGENTREE_CONFIG=os.path.join(tmp, "none.toml"),
                   AGENTREE_BENCH_FIRST_FRAME=marker, TERM=os.environ.get("TERM", "xterm-256color"))
        run_once(cmd, env, marker)              # warm-up (page cache)
        walls, inproc = [], []
        for _ in range(a.runs):
            w, i = run_once(cmd, env, marker)
            walls.append(w)
            inproc.append(i)
    med = statistics.median(walls)
    print(f"runs={a.runs} spawn->first-frame wall ms: median {med:.1f}  min {min(walls):.1f}  max {max(walls):.1f}")
    print(f"in-process (cli import -> first frame) ms: median {statistics.median(inproc):.1f}")
    print(f"target < {TARGET_MS:.0f} ms: {'OK' if med < TARGET_MS else 'OVER'}")
    return 0 if med < TARGET_MS else 3


if __name__ == "__main__":
    sys.exit(main())
