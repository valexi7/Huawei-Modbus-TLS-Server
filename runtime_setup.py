"""Compatibility import for the canonical HACS-packaged runtime setup helpers."""

import sys
from pathlib import Path

_MODULE_DIR = Path(__file__).parent / "custom_components" / "huawei_emma_management"
sys.path.append(str(_MODULE_DIR))

from embedded_runtime_setup import *  # noqa: E402,F401,F403
