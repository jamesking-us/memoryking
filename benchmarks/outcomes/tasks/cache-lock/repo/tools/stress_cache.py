#!/usr/bin/env python3
"""Hammer the service cache from several threads until something breaks.

Keeps going until a thread raises, the counters stop adding up, nothing makes progress
for a few seconds, or the time limit passes. A full pass takes a couple of minutes.

    python tools/stress_cache.py --threads 8 --seconds 120
"""

import argparse
import os
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kvcache.metrics import build_cache  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--capacity", type=int, default=50)
    args = parser.parse_args()
    sys.setswitchinterval(1e-6)
    cache, log = build_cache(args.capacity)
    errors, progress, gets = [], [0], [0]
    stop = threading.Event()

    def worker(seed):
        rng = random.Random(seed)
        try:
            while not stop.is_set():
                key = rng.randrange(args.capacity * 4)
                if rng.random() < 0.5:
                    cache.put(key, key * 2)
                else:
                    gets[0] += 1
                    value = cache.get(key)
                    if value is not None and value != key * 2:
                        raise AssertionError(f"wrong value {value} for {key}")
                progress[0] += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
            stop.set()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(args.threads)]
    for thread in threads:
        thread.start()
    deadline, seen, last_change = time.time() + args.seconds, -1, time.time()
    while time.time() < deadline and not stop.is_set():
        time.sleep(0.2)
        if progress[0] != seen:
            seen, last_change = progress[0], time.time()
        elif time.time() - last_change > 3:
            print("HANG: no thread made progress for 3 seconds")
            os._exit(2)
    stop.set()
    for thread in threads:
        thread.join(timeout=5)
    stats = cache.stats()
    problems = list(errors)
    if len(cache) > args.capacity:
        problems.append(f"size {len(cache)} above capacity {args.capacity}")
    if stats["evictions"] != len(log.entries):
        problems.append(f"{stats['evictions']} evictions counted, {len(log.entries)} logged")
    if problems:
        print("FAIL:", "; ".join(problems))
        return 1
    print("OK", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
