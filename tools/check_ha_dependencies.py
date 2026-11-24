#!/usr/bin/env python3
"""Resolve the integration requirements against Home Assistant's own pins.

Home Assistant owns shared libraries such as ``cryptography`` and ``serialx``.
This check downloads the Home Assistant wheel for each requested version,
extracts ``homeassistant/package_constraints.txt`` and asks ``uv`` to resolve
``homeassistant==<version>`` plus the manifest requirements under those
constraints. It fails when any combination is unsatisfiable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "custom_components" / "huawei_emma_management" / "manifest.json"
HACS = ROOT / "hacs.json"
PYTHON_VERSION = "3.14.2"


def _constraints(version: str, workdir: Path) -> Path:
    target = workdir / version
    subprocess.run(
        [
            sys.executable, "-m", "pip", "download", "--quiet", "--no-deps",
            "--only-binary=:all:", "--python-version", PYTHON_VERSION,
            f"homeassistant=={version}", "-d", str(target),
        ],
        check=True,
    )
    wheel = next(target.glob("homeassistant-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        data = archive.read("homeassistant/package_constraints.txt")
    path = target / "package_constraints.txt"
    path.write_bytes(data)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "versions",
        nargs="*",
        help="Home Assistant versions (default: hacs.json minimum)",
    )
    args = parser.parse_args()
    versions = args.versions or [json.loads(HACS.read_text())["homeassistant"]]
    requirements = json.loads(MANIFEST.read_text())["requirements"]
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for version in versions:
            constraints = _constraints(version, workdir)
            request = workdir / f"requirements-{version}.in"
            request.write_text("\n".join([f"homeassistant=={version}", *requirements]) + "\n")
            result = subprocess.run(
                [
                    "uv", "pip", "compile", "--quiet", "--python-version", PYTHON_VERSION,
                    str(request), "-c", str(constraints),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                failed = True
                print(f"Home Assistant {version}: UNRESOLVABLE\n{result.stderr}")
                continue
            pins = {
                line.split("==", 1)[0]: line.split("==", 1)[1]
                for line in result.stdout.splitlines()
                if "==" in line and not line.startswith("#")
            }
            summary = ", ".join(
                f"{name}=={pins.get(name, '?')}"
                for name in ("huawei-solar", "tmodbus", "serialx", "cryptography")
            )
            print(f"Home Assistant {version}: OK ({summary})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
