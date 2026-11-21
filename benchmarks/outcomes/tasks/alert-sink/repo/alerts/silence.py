"""Maintenance windows: hosts whose alerts should be suppressed for a while."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Window:
    host: str
    start: float
    end: float

    def covers(self, host, now):
        return self.host == host and self.start <= now < self.end


def active(windows, host, now):
    return any(window.covers(host, now) for window in windows)


def filter_alerts(alerts, windows, now):
    """Drop alerts for hosts that are in a maintenance window at `now`."""
    return [alert for alert in alerts if not active(windows, alert.host, now)]
