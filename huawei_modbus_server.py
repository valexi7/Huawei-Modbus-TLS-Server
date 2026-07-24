#!/usr/bin/env python3
"""Compatibility launcher for the canonical HACS-packaged server module."""

from __future__ import annotations

import sys
from pathlib import Path

_MODULE_DIR = Path(__file__).parent / "custom_components" / "huawei_emma_management"
sys.path.append(str(_MODULE_DIR))

import embedded_server as _implementation  # noqa: E402

globals().update(
    {
        name: value
        for name, value in vars(_implementation).items()
        if not name.startswith("__")
    }
)


if __name__ == "__main__":
    _implementation.main()
