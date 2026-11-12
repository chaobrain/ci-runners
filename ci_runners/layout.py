"""Locate the installed source tree independently of a component's package depth."""
from __future__ import annotations

import os


def release_root() -> str:
    """The checkout or installed release containing ``ci_runners/`` and ``bin/``."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
