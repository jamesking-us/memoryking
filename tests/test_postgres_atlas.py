"""Same atlas behaviors on a real native PostgreSQL primary with RLS."""

from test_atlas import AtlasCases
from test_postgres_parity import NativeFixture


class PostgresAtlasTests(AtlasCases, NativeFixture):
    pass
