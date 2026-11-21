# Contributing

The product is an early preview. Changes should improve current behavior and documentation without weakening authorization, recovery or evidence requirements.

## Setup

Use Python 3.12.14, Node 22.23.1 and npm 10.9.8, as recorded in the runtime files and package metadata.

```sh
python3.12 -m venv .venv
.venv/bin/python -I -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -I -m pip install --no-build-isolation --no-deps -e .
npm ci --ignore-scripts
.venv/bin/python tools/check.py
```

The checks cover generated artifacts, Python lint/format/types, TypeScript build/tests, Python tests, scoped conformance and executable documentation examples. Native PostgreSQL tests explicitly skip when its binaries are absent; a skipped backend is not verified.

To make PostgreSQL mandatory, supply an absolute `MEMORYKING_PG_BIN` directory containing `postgres`, `initdb`, `pg_ctl` and `psql`, and set `MEMORYKING_REQUIRE_POSTGRES=1`. The reference version is PostgreSQL 18.6. `tools/setup_postgres_dev.py` can build the pinned source locally; it needs the platform's compiler/build prerequisites and creates no persistent service.

The framework adapters are tested against the real frameworks in `tests/integrations/`, which `tools/check.py` does not discover; those tests skip when a framework is missing. To run them, install the frameworks into a scratch virtual environment outside the repository (they are not in the dev lock) and run `python -m unittest discover -s tests/integrations` with that environment's Python.

The semantic suite is one set of cases on two backends: every test in the knowledge, retrieval, working-state, handoff, repository, execution, atlas, inference and egress case classes also runs on native PostgreSQL through `tests/test_postgres_parity.py` and its siblings. A case that genuinely cannot run there (SQLite file backups, offline schema upgrades, fixture SQL only SQLite understands) is named in `LOCAL_ONLY` with its reason; do not widen that map to make a parity failure go away. Store operations are written in portable SQL in `src/memoryking/application/store_core.py` against the dialect in `application/storage.py`; the PostgreSQL facade refuses `INSERT OR`, `MATCH`, `BLOB`, `GLOB` and `PRAGMA`, so use `upsert`, `insert_ignore`, `dialect.lexical_match()` and `dialect.byte_length()` instead.

## Clean-checkout verification

Commit the candidate first. Substitute the full output of `git rev-parse HEAD`:

```sh
.venv/bin/python tools/local_ci.py \
  --revision FULL_40_CHARACTER_COMMIT \
  --output tmp/local-job-01 \
  --postgres-bin /absolute/path/to/postgresql/bin
```

Use a new output directory for each run. The helper builds a fresh checkout, records the actual host and commands, runs checks, builds the wheel, exercises a fresh core installation and a separate TypeScript consumer. Optional `--macos-supervision` and `--compose` rehearsals exercise the corresponding local deployment. Logs stay in ignored `tmp/` directories. Review them for secrets before sharing.

All jobs run locally. Do not enable hosted workflows or register persistent runners. No check sends email or calls a paid model provider.

## Maintaining contracts and examples

`contracts/` holds authoritative versioned schemas; generated Python, TypeScript and browser assets ship together. Run `tools/generate_contracts.py`, `npm run build`, then the full check to detect drift. Do not edit generated files alone.

Add runnable examples to [the example manifest](docs/developer-examples.json). `tools/check_docs.py` checks documentation links and executes those commands with their expected results. Use disposable state and deterministic assertions, not production data. A scenario fixture is a requirement, not proof that its behavior passed.

Keep the README short and approachable. Put user detail in the five guides it links to. Describe what is actually implemented, including unavailable optional channels and known limits; do not copy internal development diaries into the product documentation.

## Distribution

MemoryKing is licensed under the Apache License, Version 2.0 (`LICENSE`), with third-party notices in `NOTICE`; the bundled tokenizer vocabulary keeps its own MIT notice at `src/memoryking/adapters/tokenizers/LICENSE`. Both files ship inside the wheel's `dist-info/licenses`, and `tools/check_wheel.py` fails if they are missing. Contributions are accepted under the same licence.

Releasing is a local, owner-run sequence described in the [deployment guide](docs/deployment.md#publishing-a-release): `tools/build_release.py` produces a reproducible wheel and sdist with `SHA256SUMS` and `RELEASE.json`, `deploy/local/Dockerfile` builds the local-daemon image from that wheel, `tools/image_rehearsal.py` proves the image serves and records its digest, and the owner uploads, pushes and signs with credentials that never enter this repository. No hosted workflow publishes anything. A user-visible change adds a line to the **Unreleased** section of `CHANGELOG.md` in the same commit; that section becomes the release notes.

`tools/dependency_inventory.py` reads installed package metadata, license-file hashes and committed locks. Its output is an inventory, not a blanket license grant. Model downloads have their own manifest/license requirements.

## Measuring a change

Every benchmark is local and judge-free, and none calls a model. Pick the command by the kind of change; the [reference](docs/reference.md#retrieval-quality-gate) says what each one measures.

| If you change | Run | It fails when |
| --- | --- | --- |
| Ranking, candidate selection, passages or word handling | `tools/check.py` (runs `retrieval_benchmark.py --gate`, `evidence_recall.py --gate` on the 72-question sample and `stale_facts_benchmark.py --gate`), then the comparison below | a gate threshold is missed |
| How corrections, versions or replaced claims are returned | `python tools/stale_facts_benchmark.py --output report.json`; read `failing_cases` | `--gate` misses a threshold; the report names the cases still ranked below a stale statement |
| Anything that touches capture, indexing, the job queue, backup, restore or reindex | `python tools/scale_regression.py --sizes 20000 --output report.json --check`; use `--sizes 20000,100000` for a change that could cost more than a constant per row | a budget in `fixtures/evaluation/scale-budgets.json` is exceeded or any request reaches the five-second deadline |

`tools/check.py` stays under a few minutes; the 20,000-observation scale run (a few minutes more, mostly backup and restore) is a step of `tools/local_ci.py` instead, because it is for the final clean-checkout evidence, not for every edit.

To judge a ranking change against the commit it started from, measure both on the same data and compare:

```sh
# on the base commit
.venv/bin/python tools/evidence_recall.py --data fixtures/evaluation/longmemeval-regression-sample.json --output tmp/base.json
# on your change
.venv/bin/python tools/evidence_recall.py --data fixtures/evaluation/longmemeval-regression-sample.json --output tmp/mine.json --compare tmp/base.json
```

It prints every metric and every question type's before, after and delta, lists the questions that left or entered the top five sessions, and exits 1 when a metric drops by more than `--tolerance` (0.02) or a question type's by more than `--type-tolerance` (0.10). Reports measured on different data or settings are refused (exit 2). For a change that could matter on real conversations, run the same command on the full downloaded LongMemEval files (`longmemeval_oracle.json` pooled, and `longmemeval_s_cleaned.json` with `--no-pool`, see the reference for the download and the cost) and put the numbers in the change description. `--current mine.json` compares two saved reports without measuring again.

Raise a gate threshold when a change improves a metric; never lower one without a note in the pull request. The sample behind the evidence-recall gate is rebuilt with `tools/build_recall_sample.py`; change it only together with the gate file.
