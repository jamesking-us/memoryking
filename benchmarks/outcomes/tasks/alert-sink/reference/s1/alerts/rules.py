"""Evaluate threshold rules against monitoring samples."""

import operator

from alerts.model import Alert

OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}


def evaluate(samples, rules):
    alerts = []
    for host in sorted(samples):
        values = samples[host]
        for rule in sorted(rules, key=lambda r: r["name"]):
            metric = rule["metric"]
            if metric not in values:
                continue
            if OPS[rule["op"]](values[metric], rule["threshold"]):
                message = f"{metric}={values[metric]} {rule['op']} {rule['threshold']}"
                alerts.append(Alert(rule["name"], rule["severity"], host, message))
    return alerts
