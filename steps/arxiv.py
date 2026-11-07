"""arxiv-fetch and arxiv-convert steps (contract 1 in docs/contracts.md).

Usage: python3 -I arxiv.py fetch|convert <job_dir> <out_dir>
"""

import sys

PERMANENT = 20


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("fetch", "convert"):
        print("usage: arxiv.py fetch|convert <job_dir> <out_dir>", file=sys.stderr)
        return PERMANENT
    print(f"arxiv {argv[1]} is not implemented yet", file=sys.stderr)
    return PERMANENT


if __name__ == "__main__":
    sys.exit(main(sys.argv))
