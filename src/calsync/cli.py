"""Command-line entry point. Grows into the full CLI in Task 14."""

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from calsync import __version__


def main(argv: Sequence[str] | None = None, *, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="calsync", description="One-way calendar sync.")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)
    if args.version:
        out.write(f"calsync {__version__}\n")
        return 0
    parser.print_help(out)
    return 2
