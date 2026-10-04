#!/usr/bin/env python3
from __future__ import annotations

import importlib
import os
import platform
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    ("httpx", "httpx"),
    ("ddgs", "ddgs"),
    ("telegram", "python-telegram-bot"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("smolagents", "smolagents"),
]

OPTIONAL = [("dotenv", "python-dotenv"), ("pytest", "pytest")]

TARGET = {
    "python_min": (3, 11),
    "python_max": (3, 13),
}


def main() -> int:
    failures: list[str] = []
    print(f"platform: {platform.platform()}")
    print(f"machine: {platform.machine()}")
    print(f"python: {sys.version.split()[0]} ({sys.executable})")

    version = sys.version_info[:2]
    if version < TARGET["python_min"] or version > TARGET["python_max"]:
        failures.append(
            f"python {version[0]}.{version[1]} outside supported range "
            f"{TARGET['python_min'][0]}.{TARGET['python_min'][1]}"
            f"-{TARGET['python_max'][0]}.{TARGET['python_max'][1]}"
        )

    for module_name, package_name in REQUIRED:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            failures.append(f"missing {package_name}: {type(exc).__name__}: {exc}")
            continue
        version = getattr(module, "__version__", None)
        if version is None:
            try:
                from importlib.metadata import version as pkg_version

                version = pkg_version(package_name)
            except Exception:
                version = "unknown"
        print(f"ok: {package_name} {version}")

    for module_name, package_name in OPTIONAL:
        try:
            importlib.import_module(module_name)
            print(f"ok: {package_name}")
        except Exception:
            print(f"warn: {package_name} not installed")

    storage = PROJECT_ROOT / "storage"
    try:
        storage.mkdir(parents=True, exist_ok=True)
        fd, probe_name = tempfile.mkstemp(dir=storage)
        os.close(fd)
        probe = Path(probe_name)
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        print(f"ok: storage writable ({storage})")
    except Exception as exc:
        failures.append(f"storage not writable: {exc}")

    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        print("ok: .env present (values not displayed)")
        for key in ("LLM_ENDPOINTS", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS"):
            present = any(
                line.startswith(key) and line.split("=", 1)[1].strip()
                for line in env_file.read_text(encoding="utf-8").splitlines()
            )
            print(f"{'ok' if present else 'warn'}: {key} {'set' if present else 'empty'}")
    else:
        print("warn: .env missing (copy .env.example)")

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f" - {failure}")
        return 1
    print("\nenvironment OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
