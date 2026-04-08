"""SettingsStore — persists AppSettings to disk (JSONC supported) with lock/unlock semantics."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from .models import AppSettings, _settings_from_dict

logger = logging.getLogger(__name__)

_SECRET_PREFIXES = ("env:", "keyring:")


def _is_plain_key(value: str) -> bool:
    """Return True when value looks like a bare secret (not an env:/keyring: reference)."""
    return bool(value) and not any(value.startswith(p) for p in _SECRET_PREFIXES)


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
        # Track env: references per role so plain keys are written to the same var name.
        self._env_refs: dict[str, str] = {}
        self._index_env_refs(asdict(self._settings))

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _index_env_refs(self, data: dict) -> None:
        """Populate _env_refs from a settings dict (role → 'env:VAR_NAME')."""
        for role, agent_cfg in data.get("agent_configs", {}).items():
            api_key = (agent_cfg.get("model_config") or {}).get("api_key", "")
            if api_key.startswith("env:") or api_key.startswith("keyring:"):
                self._env_refs[role] = api_key

    def _env_file(self) -> Path:
        """Path of the .env file co-located with the settings file."""
        return self._SETTINGS_FILE.parent / ".env"

    def _redirect_plain_api_keys(self, data: dict) -> dict:
        """Intercept plain API keys in data before writing to disk.

        For each ``agent_configs[role].model_config.api_key`` that is a bare
        secret (not ``env:`` / ``keyring:`` prefixed):

        1. Derive the env var name:
           - Reuse the existing ``env:VAR_NAME`` stored in ``_env_refs`` for
             that role, so repeated saves always hit the same variable.
           - Otherwise generate ``OLAMO_API_KEY_<ROLE_UPPER>`` deterministically.
        2. Write / update the value in the project ``.env`` file via
           ``dotenv.set_key`` so it persists across restarts.
        3. Update ``os.environ`` so ``resolve_secret()`` sees it immediately.
        4. Replace the plain key in *data* with ``env:VAR_NAME`` so the
           settings file on disk never contains real secrets.
        """
        try:
            from dotenv import set_key as _dotenv_set_key  # type: ignore[import]
        except ImportError:
            logger.warning(
                "python-dotenv not installed — plain API keys will be written to the "
                "settings file. Run: pip install python-dotenv"
            )
            return data

        data = copy.deepcopy(data)
        env_file = self._env_file()

        for role, agent_cfg in data.get("agent_configs", {}).items():
            mc = agent_cfg.get("model_config") or {}
            api_key = mc.get("api_key", "")
            if not _is_plain_key(api_key):
                # Already an env:/keyring: reference — just keep _env_refs up to date.
                if api_key.startswith("env:") or api_key.startswith("keyring:"):
                    self._env_refs[role] = api_key
                continue

            # Determine which env var name to use.
            existing_ref = self._env_refs.get(role, "")
            if existing_ref.startswith("env:"):
                var_name = existing_ref[4:]
            else:
                var_name = f"OLAMO_API_KEY_{role.upper().replace('-', '_')}"

            # Write to .env and os.environ.
            try:
                _dotenv_set_key(str(env_file), var_name, api_key)
                os.environ[var_name] = api_key
                logger.info("API key for role %r saved to .env as %s", role, var_name)
            except Exception as exc:
                logger.warning("Failed to write API key for role %r to .env: %s", role, exc)

            # Record the reference and replace the plain key in the dict.
            self._env_refs[role] = f"env:{var_name}"
            mc["api_key"] = f"env:{var_name}"

        return data

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
        data = self._redirect_plain_api_keys(asdict(self._settings))
        self._SETTINGS_FILE.write_text(json.dumps(data, indent=2))

    # ── Public API ────────────────────────────────────────────────────────────

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
            if self._active_runs <= 0:
                logger.warning("unlock() called with _active_runs=%d — clamping to 0", self._active_runs)
                self._active_runs = 0
                return
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
