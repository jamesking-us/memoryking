"""The identical import/retry/authority assertions against native PostgreSQL."""

from test_legacy_migration import MigrationCases
from test_postgres_parity import NativeFixture


class NativeMigrationTests(MigrationCases, NativeFixture):
    pass
