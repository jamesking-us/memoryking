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

Ranking or candidate-selection changes must keep `tools/retrieval_benchmark.py --gate` and `tools/evidence_recall.py --gate` (on the committed sample) green; `tools/check.py` runs both. Report the pooled LongMemEval oracle figure from `tools/evidence_recall.py` in the change description when ranking moves. Raise a threshold in `fixtures/evaluation/retrieval-gate.json` when a change improves a metric, never lower one without a note in the pull request.
