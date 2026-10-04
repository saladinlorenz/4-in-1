from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class SecretStore:
    """File-backed secret store (``storage/.env.runtime``).

    The ONLY door for secrets. Values are never returned by the HTTP API and
    never logged: the API layer only ever sees ``mask()`` output
    (``Configured (ends ...9Kx2)``). Internal services call ``get()`` in-process.
    Replacement only: there is no "read the key back into the UI" path.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # --- internals -------------------------------------------------------

    def _load(self) -> dict[str, str]:
        data: dict[str, str] = {}
        if not self.path.exists():
            return data
        for line in self.path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name, separator, value = stripped.partition("=")
            if separator:
                data[name.strip()] = value
        return data

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{name}={value}" for name, value in sorted(data.items())]
        handle, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".env.runtime.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(lines))
                if lines:
                    stream.write("\n")
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)

    # --- public API ------------------------------------------------------

    @staticmethod
    def _check(name: str, value: str | None = None) -> str:
        clean = (name or "").strip()
        if not _NAME_RE.fullmatch(clean):
            raise ValueError("secret name must match [A-Za-z_][A-Za-z0-9_]*")
        if value is not None and ("\n" in value or "\r" in value):
            raise ValueError("secret value must be a single line")
        return clean

    def set(self, name: str, value: str) -> None:
        clean = self._check(name, value)
        if not value:
            raise ValueError("secret value must not be empty")
        data = self._load()
        data[clean] = value
        self._save(data)

    def get(self, name: str) -> str | None:
        """Internal read. Never expose the result through HTTP or prompts."""
        clean = self._check(name)
        return self._load().get(clean)

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def delete(self, name: str) -> bool:
        clean = self._check(name)
        data = self._load()
        if clean not in data:
            return False
        del data[clean]
        self._save(data)
        return True

    def mask(self, name: str) -> str | None:
        """Safe representation for the API/UI: never the value itself."""
        value = self.get(name)
        if value is None:
            return None
        suffix = value[-4:] if len(value) >= 4 else ""
        return f"Configured (ends ...{suffix})"

    def masked_items(self, names: list[str]) -> dict[str, str | None]:
        return {name: self.mask(name) for name in names}

    def names(self) -> list[str]:
        """Names only (safe to show): never the values."""
        return sorted(self._load())
