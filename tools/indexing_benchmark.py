"""How fast semantic indexing keeps up with a history, and what it costs the foreground.

It builds a fresh store with N synthetic chat records (a fixed seed; most are short turns,
some a few hundred tokens, a few longer than the 512-token limit), enables the project, and
lets the daemon's own indexing loop (`EmbeddingLoop`) index all of it through the real
claim, embed and complete path, with the model on this machine. It reports records a
second and where the time went: tokenising, the model call, the store (claim and complete
calls, including the socket) and everything else (loop and Python overhead).

With `--sessions` the records are long (4 to 30 KiB, a session's turns as paragraphs, as the
hooks store one) instead of chat turns. A record is indexed once its opening has a vector, and
a long one then gets a vector for each later passage; the report gives records a second up to
the moment every opening is done (comparable with a run of the code before passages), and,
for the code that has passages, the seconds, vectors and vectors a second until the last
passage is done.

With `--briefings K` it then repeats the run while a second thread asks for K briefings
one after another, and reports their latency at the median and the worst, next to the same
K briefings on an idle daemon.

    python tools/indexing_benchmark.py --models /tmp/mk-models --records 500 --repeats 3

`--threads` and `--batch` override the defaults of the code under test (the model's CPU
threads and the records per model call); the report names what ran. The machine is shared in
practice, so read the spread over `--repeats`, not one number. This measures the speed of
this implementation on this machine; it says nothing about the quality of the vectors
(`tools/check.py` runs the retrieval and evidence-recall gates for that).
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import platform
import random
import re
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# MEMORYKING_SRC names another source tree (a checkout of an earlier commit) to measure.
sys.path.insert(0, os.environ.get("MEMORYKING_SRC") or str(ROOT / "src"))

from memoryking.adapters.local_embeddings import LocalOnnxEmbedding  # noqa: E402
from memoryking.adapters.local_ipc import Server  # noqa: E402
from memoryking.adapters.local_store import LocalStore  # noqa: E402
from memoryking.embedding_loop import EmbeddingLoop  # noqa: E402
from memoryking.sdk import LocalClient  # noqa: E402

MODEL = "bge-small-en-v1-5-onnx"
PROJECT = "bench"


def vocabulary() -> list[str]:
    """Ordinary English words from the repository's own guides, in a stable order."""
    text = " ".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "docs").glob("*.md")))
    words = sorted(set(re.findall(r"[a-z]{3,11}", text.lower())))
    return words


def records(count: int, seed: int) -> list[str]:
    """Chat turns: 60% 15-80 words, 33% 100-300 words, 7% 450-900 words (over 512 tokens)."""
    rng = random.Random(seed)
    words = vocabulary()
    result = []
    for n in range(count):
        roll = rng.random()
        size = (
            rng.randint(15, 80)
            if roll < 0.60
            else rng.randint(100, 300)
            if roll < 0.93
            else rng.randint(450, 900)
        )
        body = " ".join(rng.choice(words) for _ in range(size))
        result.append(f"{rng.choice(['user', 'assistant'])}: turn {n}. {body}.")
    return result


def sessions(count: int, seed: int) -> list[str]:
    """Long records: alternating `user:` and `assistant:` paragraphs of 30 to 200 words, until a
    size drawn between 4 and 30 KiB is reached."""
    rng = random.Random(seed)
    words = vocabulary()
    result = []
    for n in range(count):
        target = rng.randint(4096, 30720)
        parts, size = [], 0
        while size < target:
            body = " ".join(rng.choice(words) for _ in range(rng.randint(30, 200)))
            parts.append(f"{'user' if len(parts) % 2 == 0 else 'assistant'}: {body}.")
            size += len(parts[-1]) + 2
        result.append(f"session {n}\n\n" + "\n\n".join(parts))
    return result


class Timed:
    """Delegates to an object and adds the time of the named methods to a shared total."""

    def __init__(self, target, names, totals, key):
        self.__dict__.update(target=target, names=names, totals=totals, key=key)

    def __getattr__(self, name):
        attribute = getattr(self.target, name)
        if name not in self.names:
            return attribute

        def call(*args, **kwargs):
            start = time.perf_counter()
            try:
                return attribute(*args, **kwargs)
            finally:
                self.totals[self.key] += time.perf_counter() - start

        return call


def store_vectors(store, openings=True):
    """Vectors in the store: the openings (one for each record indexed) or all of them."""
    with store.lock:
        where = " WHERE part=0" if openings else ""
        return store.db.execute("SELECT count(*) FROM embedding_vectors" + where).fetchone()[0]


def passages_pending(store):
    """Long records whose later passages still lack vectors (0 for code without passages)."""
    with store.lock:
        try:
            return store.db.execute(
                "SELECT count(*) FROM embedding_vectors WHERE part=0 AND rule=''"
            ).fetchone()[0]
        except Exception:  # an earlier schema has no passage vectors
            return 0


def build_loop(store, server, credential_file, provider, batch):
    parameters = inspect.signature(EmbeddingLoop.__init__).parameters
    extra = {"records_per_call": batch} if batch and "records_per_call" in parameters else {}
    return EmbeddingLoop(store, server.path, credential_file, provider, **extra)


def run_once(texts, args, briefings):
    """One fresh store: index every record; optionally measure briefings meanwhile."""
    original_embedding = LocalClient.embedding
    with tempfile.TemporaryDirectory(prefix="mk-ib-", dir="/tmp") as temp:
        root = Path(temp)
        credential_file = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store:
            server = Server(store)
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            try:
                kwargs = {"threads": args.threads} if args.threads else {}
                provider = LocalOnnxEmbedding(args.models / MODEL, MODEL, **kwargs)
                store.embedder = provider
                provider.embed([t[:8000] for t in texts[:4]])  # warm up the runtime
                client = LocalClient(server.path, credential_file, PROJECT)
                for n, text in enumerate(texts):
                    client.capture(
                        str(n),
                        {
                            "version": 1,
                            "project": PROJECT,
                            "source": "synthetic-development",
                            "source_event": str(n),
                            "text": text,
                            "readers": None,
                            "classification": "internal",
                            "observed_at_ms": 1,
                        },
                    )
                client.embedding(
                    {
                        "version": 1,
                        "incarnation": client.binding[-1],
                        "action": "configure",
                        "key": "config",
                        "args": {
                            "project": PROJECT,
                            "model": MODEL,
                            "state": "experimental",
                            "expected_generation": 0,
                        },
                    }
                )
                totals = {"tokenise": 0.0, "model": 0.0, "store": 0.0}
                provider.tokenizer = Timed(
                    provider.tokenizer, {"encode_batch", "encode"}, totals, "tokenise"
                )
                provider.session = Timed(provider.session, {"run"}, totals, "model")
                original = LocalClient.embedding

                def embedding(self, command):
                    start = time.perf_counter()
                    try:
                        return original(self, command)
                    finally:
                        totals["store"] += time.perf_counter() - start

                LocalClient.embedding = embedding
                latencies: list[float] = []
                done = threading.Event()

                def brief_loop():
                    query = "What did we decide about the release schedule?"
                    args_ = {
                        "project": PROJECT,
                        "query": query,
                        "bindings": [{}],
                        "tokenizer": "unknown-model",
                        "max_output_tokens": 4000,
                    }
                    reader = LocalClient(server.path, credential_file, PROJECT)
                    while len(latencies) < briefings and not done.is_set():
                        start = time.perf_counter()
                        reader.retrieval(
                            {
                                "version": 1,
                                "incarnation": reader.binding[-1],
                                "action": "brief",
                                "args": args_,
                            }
                        )
                        latencies.append((time.perf_counter() - start) * 1000)
                        time.sleep(0.2)

                loop = build_loop(store, server, credential_file, provider, args.batch)
                loop.enable_new_projects()
                watcher = None
                if briefings:
                    watcher = threading.Thread(target=brief_loop, daemon=True)
                    watcher.start()
                start = time.perf_counter()
                if hasattr(loop, "threads"):
                    # The loop as the daemon runs it: its own worker threads.
                    loop.start()
                    while store_vectors(store) < len(texts) and time.perf_counter() - start < 3600:
                        time.sleep(0.05)
                    seconds = time.perf_counter() - start
                    # The rest is passages, if the code makes them: wait for the last.
                    while passages_pending(store) and time.perf_counter() - start < 3600:
                        time.sleep(0.05)
                    total = time.perf_counter() - start
                    loop.stop()
                else:
                    while loop.tick():
                        pass
                    seconds = total = time.perf_counter() - start
                done.set()
                if watcher:
                    watcher.join()
                LocalClient.embedding = original
                vectors = store_vectors(store, openings=False)
                if store_vectors(store) != len(texts):
                    raise SystemExit(f"indexed {store_vectors(store)} of {len(texts)} records")
                split = {k: round(v, 2) for k, v in totals.items()}
                # With several worker threads the parts overlap: they are summed over the
                # threads, so they can add up to more than the elapsed time.
                split["other"] = round(seconds - sum(totals.values()), 2)
                split["busy_over_elapsed"] = round(sum(totals.values()) / seconds, 2)
                return {
                    "seconds": round(seconds, 2),
                    "records_per_second": round(len(texts) / seconds, 1),
                    "total_seconds": round(total, 2),
                    "vectors": vectors,
                    "vectors_per_second": round(vectors / total, 1),
                    "split_seconds": split,
                    "briefing_ms": latencies,
                }
            finally:
                LocalClient.embedding = original_embedding
                server.shutdown()
                serving.join(timeout=5)
                server.server_close()


def idle_briefings(texts, args, count):
    """The same briefings on a daemon whose index is complete and which has nothing to do."""
    with tempfile.TemporaryDirectory(prefix="mk-ib-", dir="/tmp") as temp:
        root = Path(temp)
        credential_file = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store:
            server = Server(store)
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            try:
                kwargs = {"threads": args.threads} if args.threads else {}
                provider = LocalOnnxEmbedding(args.models / MODEL, MODEL, **kwargs)
                store.embedder = provider
                client = LocalClient(server.path, credential_file, PROJECT)
                for n, text in enumerate(texts[:100]):
                    client.capture(
                        str(n),
                        {
                            "version": 1,
                            "project": PROJECT,
                            "source": "synthetic-development",
                            "source_event": str(n),
                            "text": text,
                            "readers": None,
                            "classification": "internal",
                            "observed_at_ms": 1,
                        },
                    )
                client.embedding(
                    {
                        "version": 1,
                        "incarnation": client.binding[-1],
                        "action": "configure",
                        "key": "config",
                        "args": {
                            "project": PROJECT,
                            "model": MODEL,
                            "state": "experimental",
                            "expected_generation": 0,
                        },
                    }
                )
                loop = build_loop(store, server, credential_file, provider, args.batch)
                while loop.tick():
                    pass
                latencies = []
                for _ in range(count):
                    start = time.perf_counter()
                    client.retrieval(
                        {
                            "version": 1,
                            "incarnation": client.binding[-1],
                            "action": "brief",
                            "args": {
                                "project": PROJECT,
                                "query": "What did we decide about the release schedule?",
                                "bindings": [{}],
                                "tokenizer": "unknown-model",
                                "max_output_tokens": 4000,
                            },
                        }
                    )
                    latencies.append((time.perf_counter() - start) * 1000)
                    time.sleep(0.2)
                return latencies
            finally:
                server.shutdown()
                serving.join(timeout=5)
                server.server_close()


def summary(values):
    ordered = sorted(values)
    return {
        "median_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))], 1),
        "worst_ms": round(ordered[-1], 1),
        "count": len(ordered),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--records", type=int, default=500)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--threads", type=int, default=0, help="model threads (0: code default)")
    parser.add_argument("--batch", type=int, default=0, help="records per call (0: code default)")
    parser.add_argument(
        "--sessions", action="store_true", help="long records (4 to 30 KiB) instead of chat turns"
    )
    parser.add_argument("--briefings", type=int, default=0, help="briefings to time (0: none)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    texts = (sessions if args.sessions else records)(args.records, args.seed)
    runs = [run_once(texts, args, 0) for _ in range(args.repeats)]
    rates = [r["records_per_second"] for r in runs]
    result = {
        "machine": {
            "platform": platform.platform(),
            "cpus": os.cpu_count(),
            "load_average": [round(x, 2) for x in os.getloadavg()],
        },
        "source": str(Path(sys.modules["memoryking"].__file__).parent),
        "records": args.records,
        "sample": "sessions" if args.sessions else "turns",
        "seed": args.seed,
        "threads": args.threads or "default",
        "batch": args.batch or "default",
        "runs": runs,
        "records_per_second": {
            "median": round(statistics.median(rates), 1),
            "min": min(rates),
            "max": max(rates),
        },
        "to_the_last_passage": {
            "seconds_median": round(statistics.median(r["total_seconds"] for r in runs), 1),
            "vectors": runs[0]["vectors"],
            "vectors_per_second_median": round(
                statistics.median(r["vectors_per_second"] for r in runs), 1
            ),
        },
    }
    if args.briefings:
        idle = idle_briefings(texts, args, args.briefings)
        busy = run_once(texts, args, args.briefings)
        result["briefing_latency"] = {
            "idle": summary(idle),
            "while_indexing": summary(busy["briefing_ms"]),
            "indexing_rate_during": busy["records_per_second"],
        }
    text = json.dumps(result, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
