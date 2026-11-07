"""Every step script rejects bad arguments with exit code 20 (contract 1)."""

import pathlib
import subprocess
import sys
import unittest

STEPS = pathlib.Path(__file__).resolve().parents[2] / "steps"


class CliTest(unittest.TestCase):
    def test_bad_arguments_exit_permanent(self) -> None:
        for script in ("arxiv.py", "feed.py", "whisper.py"):
            with self.subTest(script=script):
                run = subprocess.run(
                    [sys.executable, "-I", str(STEPS / script), "bogus"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(run.returncode, 20)
                self.assertIn("usage:", run.stderr)


if __name__ == "__main__":
    unittest.main()
