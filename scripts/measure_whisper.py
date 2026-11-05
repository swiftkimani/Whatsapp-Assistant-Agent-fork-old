#!/usr/bin/env python3
"""What one faster-whisper model size actually costs this container.

Run it on the host the numbers are about. Nothing in docker-compose.yaml's
memory reasoning can be checked from a laptop: glibc's allocator, which is
where the retention the compose comments argue about comes from, is not the one
macOS uses, and the model is loaded by the same ctranslate2 wheel either way.

    python3 scripts/measure_whisper.py --sizes tiny base small
    python3 scripts/measure_whisper.py --sizes base --audio note.ogg

Each size is measured in a fresh subprocess, because a process that has already
loaded one model has already grown the arenas the next one would reuse. Three
numbers per size: resident after the import, resident after the load, and what
came back after the model was dropped -- that last one is the one that decides
whether MODEL_IDLE_MINUTES buys anything real.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import time
from pathlib import Path

MIB = 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parent.parent
# Run from anywhere: this script lives one level down from the package it loads.
sys.path.insert(0, str(REPO_ROOT))


def rss_bytes() -> int:
    """Current resident set size, from the kernel rather than from Python."""
    if sys.platform == "linux":
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        return 0
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip()) * 1024


def peak_bytes() -> int:
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Darwin reports bytes, Linux kilobytes.
    return peak if sys.platform == "darwin" else peak * 1024


def weights_on_disk(size: str) -> int:
    """model.bin in the HF cache: the int8 weights, before any allocator."""
    home = Path(os.getenv("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    hub = home / "hub" if (home / "hub").is_dir() else home
    found = list(hub.glob(f"models--*faster-whisper-{size}/snapshots/*/model.bin"))
    return found[0].stat().st_size if found else 0


def measure_one(size: str, audio: list[str]) -> dict:
    from app import stt

    baseline = rss_bytes()
    t0 = time.monotonic()
    name, _ = stt._acquire_model(size)
    load_seconds = time.monotonic() - t0
    stt._release_model(name)
    loaded = rss_bytes()

    transcripts, seconds = [], []
    for path in audio:
        data = Path(path).read_bytes()
        t0 = time.monotonic()
        transcripts.append(stt.transcribe_bytes(data, language="en", model_size=size))
        seconds.append(round(time.monotonic() - t0, 2))
    after_use = rss_bytes()

    # What the eviction in app/stt.py gives back, run the way app/stt.py runs
    # it: backdate the last use past the idle window and let _evict_idle do the
    # rest, malloc_trim included.
    stt._last_used[size] = time.monotonic() - (stt.idle_window_seconds() + 60)
    with stt._lock:
        stt._evict_idle(time.monotonic())
    gc.collect()
    evicted = rss_bytes()

    return {
        "size": size,
        "weights_on_disk_mib": round(weights_on_disk(size) / MIB, 1),
        "rss_before_load_mib": round(baseline / MIB, 1),
        "rss_after_load_mib": round(loaded / MIB, 1),
        "load_cost_mib": round((loaded - baseline) / MIB, 1),
        "rss_after_transcribing_mib": round(after_use / MIB, 1),
        "rss_after_eviction_mib": round(evicted / MIB, 1),
        "returned_by_eviction_mib": round((after_use - evicted) / MIB, 1),
        "peak_rss_mib": round(peak_bytes() / MIB, 1),
        "load_seconds": round(load_seconds, 2),
        "transcribe_seconds": seconds,
        "transcripts": transcripts,
        "platform": sys.platform,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sizes", nargs="+", default=["tiny", "base", "small"])
    ap.add_argument("--audio", nargs="*", default=[])
    ap.add_argument("--only", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.only:
        print(json.dumps(measure_one(args.only, args.audio)))
        return 0

    repo = Path(__file__).resolve().parent.parent
    rows = []
    for size in args.sizes:
        done = subprocess.run([sys.executable, __file__, "--only", size, "--audio", *args.audio],
                              cwd=repo, capture_output=True, text=True)
        if done.returncode != 0:
            print(f"{size}: failed\n{done.stderr}", file=sys.stderr)
            return 1
        rows.append(json.loads(done.stdout.strip().splitlines()[-1]))

    head = f"{'size':<6} {'on disk':>9} {'load cost':>10} {'after use':>10} {'freed':>8} {'load s':>7} {'transcribe s':>13}"
    print(head)
    print("-" * len(head))
    for r in rows:
        secs = ", ".join(str(s) for s in r["transcribe_seconds"]) or "-"
        print(f"{r['size']:<6} {r['weights_on_disk_mib']:>8} {r['load_cost_mib']:>9} "
              f"{r['rss_after_transcribing_mib']:>9} {r['returned_by_eviction_mib']:>7} "
              f"{r['load_seconds']:>7} {secs:>13}")
    print(f"\n(MiB, measured on {rows[0]['platform']}; 'after use' is total process RSS)")
    print(json.dumps(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
