import test_postgres_parity as pg
import test_repository as repository
from test_processing import ProcessingCases
from test_processing_repository import ProcessingRepositoryCases


class NativeProcessingTests(ProcessingCases, pg.NativeFixture):
    pass


class NativeProcessingRepositoryTests(ProcessingRepositoryCases, pg.NativeFixture):
    def setUp(self):
        super().setUp()
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
    NativeProcessingRepositoryTests,
    repository.RepositoryTests,
    "git capture binding bind verify worker",
)
