"""Trusted Linux bootstrap: confine before optional libraries or report parsing."""

from __future__ import annotations

import json
import os
import resource
import runpy
import signal
import sys
from pathlib import Path

from memoryking.domain.local import LocalError
from memoryking.domain.reporting import ARTIFACT_LIMIT, BUILD_TIMEOUT
from memoryking.reporting.linux import seal


def main() -> None:
    stage, parent, paths = Path(sys.argv[1]), int(sys.argv[2]), json.loads(sys.argv[3])
    arguments = sys.argv[4:]
    os.umask(0o077)
    signal.alarm(BUILD_TIMEOUT)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (ARTIFACT_LIMIT, ARTIFACT_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (BUILD_TIMEOUT, BUILD_TIMEOUT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    seal(stage, expected_parent=parent, protected=[Path(p) for p in paths])
    if arguments == ["-m", "memoryking.reporting.worker", str(stage)]:
        sys.argv = ["memoryking.reporting.worker", str(stage)]
        runpy.run_module("memoryking.reporting.worker", run_name="__main__")
    elif len(arguments) >= 2 and arguments[0] == "-c":
        # Only isolated_python's trusted test/probe callers supply Python here.
        # The report service always uses the fixed module branch above.
        sys.argv = ["-c", *arguments[2:]]
        exec(compile(arguments[1], "<renderer-probe>", "exec"), {"__name__": "__main__"})
    else:
        raise LocalError("invalid_renderer_entry")


if __name__ == "__main__":
    main()
