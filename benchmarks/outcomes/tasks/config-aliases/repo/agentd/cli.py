"""Command line entry: `python -m agentd.cli --config agentd.json`."""

import argparse
import sys

from agentd import config
from agentd.runner import Runner


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentd")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        settings = config.load(args.config)
    except config.ConfigError as exc:
        print(f"agentd: {exc}", file=sys.stderr)
        return 2
    print(Runner(settings).describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
