#!/usr/bin/env python3
"""Adapt the browser fixture's duplex FD 3 endpoint to the facade key pipe."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    secret = os.read(3, 32)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, secret)
    finally:
        os.close(write_fd)
    os.close(3)
    os.dup2(read_fd, 3)
    os.close(read_fd)
    binary = Path(__file__).parents[3] / ".venv/bin/nan-fung-agent-tools"
    completed = subprocess.run(
        [str(binary), *sys.argv[1:]],
        check=False,
        close_fds=True,
        pass_fds=(3,),
    )
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
