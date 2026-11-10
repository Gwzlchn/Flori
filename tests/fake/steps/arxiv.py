"""Fake arxiv.py for executor tests; same action file and env dump rules as tests/fake/step."""

import json
import os
import pathlib
import sys
import time


def main(argv: list[str]) -> int:
    sub, job_dir, out_dir = argv[1], pathlib.Path(argv[2]), pathlib.Path(argv[3])
    name = f"arxiv-{sub}"
    attempt = int(os.environ.get("FLORI_ATTEMPT", "1"))
    action = "0"
    fake = job_dir / "fake"
    plan = fake / name
    if plan.exists():
        actions = plan.read_text().split()
        action = actions[attempt - 1] if len(actions) >= attempt else actions[-1]
    if fake.is_dir():
        (fake / f"{name}.env").write_text("".join(f"{k}={v}\n" for k, v in os.environ.items()))
    print(f"fake {name} attempt {attempt} action {action}", flush=True)
    if action == "hang":
        time.sleep(300)
    if action != "0":
        return int(action)
    manifest = {"title": None, "files": {}, "assets": [], "meta": {}}
    (out_dir / "manifest.json").write_text(json.dumps(manifest) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
