"""Rolling points up into per-name summaries."""

from collections import defaultdict


def rollup(points):
    """Per metric name: count, minimum, maximum and mean, sorted by name."""
    grouped = defaultdict(list)
    for point in points:
        grouped[point["name"]].append(point["value"])
    return {
        name: {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "mean": sum(values) / len(values),
        }
        for name, values in sorted(grouped.items())
    }


def latest(points):
    """The most recent point for each name."""
    newest = {}
    for point in points:
        if point["name"] not in newest or point["ts"] >= newest[point["name"]]["ts"]:
            newest[point["name"]] = point
    return newest
