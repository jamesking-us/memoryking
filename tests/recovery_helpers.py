"""Exercise each real archive adapter; no state, authorization or database mock."""

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.postgres_store import PostgresStore


def backup_fixture(fixture):
    archive = fixture.root / "backup"
    if isinstance(fixture.store, PostgresStore):
        import psycopg

        from memoryking.adapters.postgres_recovery import backup

        with psycopg.connect(fixture.cluster.dsn("mk_backup"), autocommit=True) as connection:
            backup(fixture.store, connection, archive)
    else:
        fixture.store.backup(archive)
    return archive


def restore_fixture(fixture, archive):
    native = isinstance(fixture.store, PostgresStore)
    fixture.store.close()
    if native:
        import psycopg
        from test_postgres_parity import InspectionSQL

        from memoryking.adapters.postgres_recovery import restore

        with psycopg.connect(fixture.cluster.dsn("mk_migration"), autocommit=True) as connection:
            fixture.owner = restore(
                connection, fixture.data, fixture.control, fixture.tenant, archive
            ).read_text()
        fixture.store = PostgresStore(
            fixture.pool, fixture.data, fixture.control, fixture.tenant, fixture.owner
        )
        fixture.store.db = InspectionSQL(fixture.store, fixture.store.db)
    else:
        fixture.owner = LocalStore.restore(fixture.data, fixture.control, archive).read_text()
        fixture.store = LocalStore(fixture.data, fixture.control)
    fixture.k = Knowledge(fixture.store)
