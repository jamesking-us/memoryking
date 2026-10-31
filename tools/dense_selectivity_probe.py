"""Authorized exact-search benchmark on 1,000 synthetic relational fixture vectors.

Dataset setup uses one real capture and explicit fixture rows. This measures
filter/rank correctness and latency, not ingestion throughput or model quality.
"""

import argparse
import json
import platform
import statistics
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from memoryking.adapters.embedding_store import Embeddings
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.retrieval import Retrieval
from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.embeddings import cosine_ppm


def released(adapter, credential, command):
    output = []
    adapter.execute(credential, command).emit(output.append)
    return json.loads(output[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["local", "shared"], required=True)
    parser.add_argument(
        "--engine", choices=["python_exact", "pgvector_exact"], default="python_exact"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new evidence path")
    with ExitStack() as stack:
        temp = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="mk-dense-", dir="/tmp"))
        )
        data, control = temp / "data", temp / "control"
        if args.backend == "shared":
            import psycopg
            from postgres_dev_cluster import DevCluster

            from memoryking.adapters.postgres_admin import (
                enable_vector,
                initialize_tenant,
                migrate,
                provision_roles,
            )
            from memoryking.adapters.postgres_pool import create_pool
            from memoryking.adapters.postgres_store import PostgresStore

            cluster = stack.enter_context(DevCluster())
            with psycopg.connect(cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
                if args.engine == "pgvector_exact":
                    enable_vector(admin)
            with psycopg.connect(cluster.dsn("mk_migration"), autocommit=True) as migration:
                migrate(migration)
                credential = initialize_tenant(migration, data, control, "probe").read_text()
            pool = create_pool(cluster.dsn("mk_runtime"))
            stack.callback(pool.close)
            store = stack.enter_context(PostgresStore(pool, data, control, "probe", credential))
        else:
            credential = LocalStore.initialize(data, control).read_text()
            store = stack.enter_context(LocalStore(data, control))
        tenant = store._decode(credential, "credential")["tenant"]
        readers = {
            count: store.grant(credential, f"r{count}", "probe", ["read"])
            for count in (1000, 100, 10, 1)
        }
        captured = store.capture(
            credential,
            store.namespace(credential, "probe"),
            "initial",
            {
                "version": 1,
                "project": "probe",
                "source": "synthetic",
                "source_event": "initial",
                "text": "A synthetic observation for eligibility measurement.",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        config = released(
            Embeddings(store),
            credential,
            {
                "version": 1,
                "incarnation": store.incarnation,
                "action": "configure",
                "key": "configure",
                "args": {
                    "project": "probe",
                    "model": "minilm-l6-v2-onnx",
                    "state": "experimental",
                    "expected_generation": 0,
                },
            },
        )
        vectors = {}
        with store.transaction():
            original = dict(
                store.db.execute(
                    "SELECT * FROM observations WHERE tenant=? AND id=?",
                    (tenant, captured["observation"]),
                ).fetchone()
            )
            # Fixture content is intentionally identical, with different audiences
            # and vector geometry. Hidden high scores must not consume the limit.
            for index in range(1000):
                row = dict(original)
                row["id"] = captured["observation"] if index == 0 else f"obs_fixture_{index:04}"
                row["source_event"] = f"fixture_{index}"
                row["readers"] = json.dumps(
                    ["owner"] + [f"r{count}" for count in readers if index < count]
                )
                if index == 0:
                    store.db.execute(
                        "UPDATE observations SET readers=? WHERE tenant=? AND id=?",
                        (row["readers"], tenant, row["id"]),
                    )
                else:
                    store.db.execute(
                        "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        tuple(row.values()),
                    )
                vector = [100000 + index * 900, 900000 - index * 800] + [0] * 382
                vectors[row["id"]] = vector
                store.db.execute(
                    "INSERT INTO embedding_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tenant,
                        "probe",
                        config["fingerprint"],
                        1,
                        row["id"],
                        store.incarnation,
                        1,
                        row["blob"],
                        canonical_hash("A synthetic observation for eligibility measurement."),
                        json.dumps(vector),
                        0,
                        50,
                        None,
                        0,
                        row["position"],
                    ),
                )
            source = store.db.execute(
                "SELECT position FROM tenants WHERE tenant=?", (tenant,)
            ).fetchone()[0]
            store.db.execute(
                "UPDATE embedding_partitions SET scheduled=?,frontier=? WHERE tenant=?",
                (source, source, tenant),
            )
        records = []
        vector = [1000000] + [0] * 383
        for eligible, reader in readers.items():
            durations = []
            expected = sorted(
                list(vectors)[:eligible],
                key=lambda reference: (-cosine_ppm(vector, vectors[reference]), reference),
            )[:32]
            for _sample in range(5):
                if args.backend == "shared":
                    actor = PostgresStore(pool, data, control, tenant, reader)
                else:
                    actor = store
                try:
                    started = time.monotonic()
                    result = released(
                        Retrieval(actor),
                        reader,
                        {
                            "version": 1,
                            "incarnation": store.incarnation,
                            "action": "brief",
                            "args": {
                                "project": "probe",
                                "query": "unmatchedword",
                                "bindings": [{}],
                                "tokenizer": "unknown-model",
                                "max_output_tokens": 65536,
                                "top_k": 32,
                                "dense": {
                                    "fingerprint": config["fingerprint"],
                                    "query_hash": canonical_hash("unmatchedword"),
                                    "vector": vector,
                                    "minimum_similarity_ppm": 0,
                                    "engine": args.engine,
                                },
                            },
                        },
                    )
                    durations.append(round((time.monotonic() - started) * 1000, 3))
                    assert result["coverage"]["dense"] == "ready", result["coverage"]
                    actual = [item["reference"] for item in result["guidance"]]
                    assert actual == expected, {"actual": actual, "expected": expected}
                finally:
                    if actor is not store:
                        actor.close()
            records.append(
                {
                    "eligible": eligible,
                    "selectivity_percent": eligible / 10,
                    "samples_ms": durations,
                    "median_ms": statistics.median(durations),
                    "max_ms": max(durations),
                    "expected_top_k_recall": 1,
                    "unauthorized_results": 0,
                }
            )
        result = {
            "passed": True,
            "scope": __doc__,
            "backend": args.backend,
            "engine": args.engine,
            "platform": platform.platform(),
            "corpus_size": 1000,
            "fixture_hash": canonical_hash(list(vectors.values())),
            "results": records,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))


if __name__ == "__main__":
    main()
