# alerts

Turns monitoring samples into alerts and delivers them.

    alerts/model.py          the Alert type
    alerts/sinks/email.py    e-mail delivery
    alerts/sinks/webhook.py  incident webhook delivery
    tests/                   run with: python -m unittest discover -s tests

Samples look like `{"web-1": {"cpu": 95, "disk": 40}}`; a rule looks like
`{"name": "cpu-high", "metric": "cpu", "op": ">", "threshold": 90, "severity": "critical"}`.
