from dataclasses import dataclass

SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True)
class Alert:
    rule: str
    severity: str
    host: str
    message: str

    def __post_init__(self):
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}")
