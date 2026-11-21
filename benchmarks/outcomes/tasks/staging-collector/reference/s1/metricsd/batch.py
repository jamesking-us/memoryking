"""Splitting points into request-sized batches."""


def chunks(points, size=50):
    if size < 1:
        raise ValueError("size must be at least 1")
    points = list(points)
    for start in range(0, len(points), size):
        yield points[start : start + size]
