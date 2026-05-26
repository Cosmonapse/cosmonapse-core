#!/usr/bin/env python3
"""
cosmonapse-core/install.py
==========================

One-shot installer for the Cosmonapse toolchain.

Running this script:

    python cosmonapse-core/install.py

is equivalent to:

    pip install -e cosmonapse-core/packages/python-sdk
    python -m cosmo._install        # adds Scripts/bin dir to PATH

After it finishes, `import cosmonapse` works, the `cosmo` command is on
PATH in every new terminal, and editing source under
``cosmonapse-core/packages/`` is picked up immediately (editable install).

Flags
-----
--user            forward `--user` to pip (install into the user site-packages)
--no-path         don't touch PATH; only run pip
--python <exe>    use a specific python interpreter (defaults to the one
                  running this script)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
SDK_DIR = HERE / "packages" / "python-sdk"


def run(cmd: list[str]) -> None:
    print("[cosmonapse]  $ " + " ".join(cmd))
    subprocess.check_call(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Cosmonapse SDK + CLI in editable mode and update PATH.")
    parser.add_argument("--user", action="store_true", help="pass --user to pip")
    parser.add_argument("--no-path", action="store_true", help="skip PATH modification")
    parser.add_argument("--python", default=sys.executable, help="python interpreter to install into")
    args = parser.parse_args()

    python = shutil.which(args.python) or args.python
    if not Path(python).exists() and shutil.which(python) is None:
        print(f"[cosmonapse] Could not find python interpreter: {python}", file=sys.stderr)
        return 1

    if not SDK_DIR.is_dir():
        print(f"[cosmonapse] Expected SDK at {SDK_DIR} — is your checkout intact?", file=sys.stderr)
        return 1

    # 1) Editable install of the SDK (which bundles the cosmo CLI).
    pip_cmd: list[str] = [python, "-m", "pip", "install", "-e", str(SDK_DIR)]
    if args.user:
        pip_cmd.insert(4, "--user")
    run(pip_cmd)

    # 2) Push the resulting scripts dir onto persistent PATH so `cosmo` is
    #    callable from any new terminal.
    if args.no_path:
        print("[cosmonapse] Skipping PATH update (--no-path).")
    else:
        run([python, "-m", "cosmo._install"])

    print()
    print("[cosmonapse] Done. Open a new terminal and try:  cosmo --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
