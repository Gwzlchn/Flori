"""Feed listing for a collection (contract 3 in docs/contracts.md).

Usage: python3 -I feed.py list <kind> <normalized_url> --limit N
Prints {"title": str, "items": [...]} on stdout; exit codes follow contract 1.
"""

import sys

PERMANENT = 20


def main(argv: list[str]) -> int:
    if len(argv) != 6 or argv[1] != "list" or argv[4] != "--limit":
        print("usage: feed.py list <kind> <normalized_url> --limit N", file=sys.stderr)
        return PERMANENT
    print("feed listing is not implemented yet", file=sys.stderr)
    return PERMANENT


if __name__ == "__main__":
    sys.exit(main(sys.argv))
