"""SettingsStore — persists AppSettings to disk (JSONC supported) with lock/unlock semantics."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from .models import AppSettings, _settings_from_dict

logger = logging.getLogger(__name__)


class SettingsStore:
    _SETTINGS_FILE = Path("olamo-settings.json")

    def __init__(self, settings_file: Path | None = None) -> None:
        # Resolve to absolute immediately so later chdir() calls don't affect writes.
        # Fall back to the class-level default (which tests can monkeypatch).
        if settings_file is not None:
            self._SETTINGS_FILE = Path(settings_file).resolve()
        else:
            self._SETTINGS_FILE = type(self)._SETTINGS_FILE.resolve()
        self._settings = self._load()
        self._active_runs = 0   # ref-count: lock() increments, unlock() decrements
        self._pending: AppSettings | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _strip_jsonc_comments(text: str) -> str:
        """Strip // line comments and /* block comments */ from JSONC text."""
        result: list[str] = []
        i = 0
        in_string = False
        while i < len(text):
            if in_string:
                result.append(text[i])
                if text[i] == '\\':
                    i += 1
                    if i < len(text):
                        result.append(text[i])
                elif text[i] == '"':
                    in_string = False
                i += 1
            elif text[i] == '"':
                in_string = True
                result.append(text[i])
                i += 1
            elif text[i:i+2] == '//':
                # Skip until end of line
                i = text.find('\n', i)
                if i == -1:
                    break
            elif text[i:i+2] == '/*':
                end = text.find('*/', i + 2)
                i = end + 2 if end != -1 else len(text)
            else:
                result.append(text[i])
                i += 1
        return ''.join(result)

    def _load(self) -> AppSettings:
        if self._SETTINGS_FILE.exists():
            try:
                raw = self._SETTINGS_FILE.read_text()
                cleaned = self._strip_jsonc_comments(raw)
                data = json.loads(cleaned)
                return _settings_from_dict(data)
            except Exception as exc:
                logger.warning(
                    "Failed to load settings from %s: %s — using defaults",
                    self._SETTINGS_FILE,
                    exc,
                )
        return AppSettings()

    def _save(self) -> None:
        self._SETTINGS_FILE.write_text(json.dumps(asdict(self._settings), indent=2))

    @property
    def settings(self) -> AppSettings:
        return self._settings

    @property
    def is_locked(self) -> bool:
        return self._active_runs > 0

    async def lock(self) -> None:
        async with self._lock:
            self._active_runs += 1

    async def unlock(self) -> None:
        async with self._lock:
            self._active_runs -= 1
            if self._active_runs == 0 and self._pending is not None:
                self._settings = self._pending
                self._pending = None
                self._save()

    async def try_update(self, new_settings: AppSettings) -> bool:
        async with self._lock:
            if self._active_runs > 0:
                self._pending = new_settings
                return False
            self._settings = new_settings
            self._save()
            return True
