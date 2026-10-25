import test_postgres_parity as pg
import test_repository as repository
from test_experience import ProcedureCases
from test_experience_records import RecordCases
from test_experience_repository import ProcedureRepositoryCases
from test_ranking import RankingCases


class NativeProcedureTests(ProcedureCases, pg.NativeFixture):
    pass


class NativeProcedureRepositoryTests(ProcedureRepositoryCases, pg.NativeFixture):
    def setUp(self):
        pg.NativeFixture.setUp(self)
        self.repo = self.root / "code"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / "a.py").write_text("def value():\n    return 1\n")
        (self.repo / "b.py").write_text("other = 2\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.rid = self.run_repo(
            "register", remote="local:fixture", clone="first", path=str(self.repo), readers=None
        )["repository"]
        self.first = self.capture()
        self.claim = self.propose()
        self.review(self.claim)

    run_repo = pg.PostgresRepositoryParity.run_repo


pg.reuse(
    NativeProcedureRepositoryTests,
    repository.RepositoryTests,
    "run_k body propose review query git capture binding bind verify worker",
)


class NativeRecordTests(RecordCases, pg.NativeFixture):
    pass


class NativeRankingTests(RankingCases, pg.NativeFixture):
    pass
