"""Sending points to the staging collector."""

from metricsd.batch import chunks
from metricsd.envutil import flag_enabled, require

URL = "http://collector.staging.internal:9411/v4/ingest"
FLAG = "ENABLE_COLLECTOR_V2"
SCHEMA_VERSION = 4


def staging_sender(env, post):
    def send(points):
        if not flag_enabled(env, FLAG):
            return 0
        headers = {"X-Collector-Key": require(env, "COLLECTOR_KEY")}
        sent = 0
        for batch in chunks(points):
            post(URL, headers, {"schema_version": SCHEMA_VERSION, "points": batch})
            sent += len(batch)
        return sent

    return send
