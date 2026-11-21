"""CSV rendering of export rows."""


def render_csv(rows):
    lines = ["account,total_usd"]
    lines += [f"{name},{total}" for name, total in rows]
    return "\n".join(lines) + "\n"
