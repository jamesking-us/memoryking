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

The repository remains private. No project distribution license has been selected and no package/image is published. Public sharing requires the owner to select a license and review dependency notices and release readiness. Existing third-party notices, including the bundled tokenizer license, must remain intact.

`tools/dependency_inventory.py` reads installed package metadata, license-file hashes and committed locks. Its output is an inventory, not a blanket license grant. Model downloads have their own manifest/license requirements.
