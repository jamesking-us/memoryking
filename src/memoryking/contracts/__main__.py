"""Developer contract inspector, not the future memory service CLI."""

import argparse
from pathlib import Path

from . import canonical_hash, load_strict, validate_contract


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract")
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    value = load_strict(args.file.read_bytes())
    validate_contract(args.contract, value)
    print(canonical_hash(value))


if __name__ == "__main__":
    main()
