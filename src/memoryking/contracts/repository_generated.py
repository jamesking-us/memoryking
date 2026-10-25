"""Generated repository requests; runtime schema is authoritative."""
from __future__ import annotations

from typing import Any, Literal, TypedDict


class Register(TypedDict):
    project: str
    remote: str
    clone: str
    path: str
    readers: None | list[str]

class Capture(TypedDict):
    project: str
    repository: str
    clone: str
    environment: str

class Inspect(TypedDict):
    project: str
    repository: str
    snapshot: str

class Dependency(TypedDict):
    kind: Literal["file", "symbol"]
    target: str
    policy: Literal["invalidate", "revalidate", "rank_penalty"]

class Bind(TypedDict):
    project: str
    revision: str
    repository: str
    snapshot: str
    dependencies: list[Dependency]
    impact: Literal["high", "low"]
    coverage_reviewed: Literal[True]
    expected_policy: int
    evidence: str

class Verify(TypedDict):
    project: str
    revision: str
    snapshot: str
    expected_policy: int
    evidence: str
    passed: bool
    expires_ms: int

class Revalidate(TypedDict):
    project: str
    repository: str
    snapshot: str
    expected_generation: int

class Impact(TypedDict):
    project: str
    repository: str
    snapshot: str
    paths: list[str]
    limit: int

class Coverage(TypedDict):
    project: str
    repository: str
    snapshot: str
    paths: list[str]
    bindings: list[dict[str, Any]]

class Pending(TypedDict):
    project: str
    repository: str

class History(TypedDict):
    project: str
    revision: str
    snapshot: str

class Command0(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["register"]
    args: Register
    key: str

class Command1(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["capture"]
    args: Capture
    key: str

class Command2(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["inspect"]
    args: Inspect

class Command3(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["bind"]
    args: Bind
    key: str

class Command4(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["verify"]
    args: Verify
    key: str

class Command5(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["revalidate"]
    args: Revalidate
    key: str

class Command6(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["impact"]
    args: Impact

class Command7(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["coverage"]
    args: Coverage

class Command8(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["pending"]
    args: Pending

class Command9(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["history"]
    args: History

RepositoryCommand = Command0 | Command1 | Command2 | Command3 | Command4 | Command5 | Command6 | Command7 | Command8 | Command9
