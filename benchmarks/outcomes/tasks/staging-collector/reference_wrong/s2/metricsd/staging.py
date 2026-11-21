"""Sending points to the staging collector."""

from metricsd.batch import chunks

URL = "http://localhost:8080/ingest"
SCHEMA_VERSION = 1


def staging_sender(env, post):
    def send(points):
        if env.get("ENABLE_STAGING_COLLECTOR") != "1":
            return 0
        headers = {"Authorization": "Bearer " + env.get("COLLECTOR_TOKEN", "")}
        sent = 0
        for batch in chunks(points):
            post(URL, headers, {"schema_version": SCHEMA_VERSION, "points": batch})
            sent += len(batch)
        return sent

    return send
