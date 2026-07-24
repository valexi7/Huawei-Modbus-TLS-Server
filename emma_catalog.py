"""Compatibility import for the canonical HACS-packaged register catalog."""

import sys
from pathlib import Path

_MODULE_DIR = Path(__file__).parent / "custom_components" / "huawei_emma_management"
sys.path.append(str(_MODULE_DIR))

from embedded_catalog import *  # noqa: E402,F401,F403
