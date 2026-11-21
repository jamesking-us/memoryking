# metricsd

Helpers for shipping application metrics.

    metricsd/points.py    building and validating points
    metricsd/envutil.py   reading flags and settings from an environment mapping
    tests/                run with: python -m unittest discover -s tests

HTTP calls are made through an injected `post(url, headers, payload)` callable so that tests
need no network.
