from test_delta import DeltaCases
from test_postgres_parity import NativeFixture


class NativeDeltaTests(DeltaCases, NativeFixture):
    pass
