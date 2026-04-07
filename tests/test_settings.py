"""Tests for app.settings.SettingsStore."""

import json

import pytest

from app.models import AppSettings
from app.settings import SettingsStore


class TestSettingsStore:
    @pytest.fixture(autouse=True)
    def _isolate_settings_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(SettingsStore, "_SETTINGS_FILE", tmp_path / "settings.json")

    @pytest.mark.asyncio
    async def test_starts_unlocked_with_defaults(self):
        store = SettingsStore()
        assert not store.is_locked
        assert store.settings == AppSettings()

    @pytest.mark.asyncio
    async def test_lock_sets_locked(self):
        store = SettingsStore()
        await store.lock()
        assert store.is_locked

    @pytest.mark.asyncio
    async def test_unlock_clears_locked(self):
        store = SettingsStore()
        await store.lock()
        await store.unlock()
        assert not store.is_locked

    @pytest.mark.asyncio
    async def test_try_update_applies_when_not_locked(self):
        store = SettingsStore()
        new = AppSettings(pm_model="opus")
        applied = await store.try_update(new)
        assert applied is True
        assert store.settings.pm_model == "opus"

    @pytest.mark.asyncio
    async def test_try_update_queues_and_returns_false_when_locked(self):
        store = SettingsStore()
        await store.lock()
        new = AppSettings(pm_model="opus")
        applied = await store.try_update(new)
        assert applied is False
        assert store.settings.pm_model != "opus"

    @pytest.mark.asyncio
    async def test_unlock_applies_pending_settings(self):
        store = SettingsStore()
        await store.lock()
        await store.try_update(AppSettings(pm_model="opus"))
        await store.unlock()
        assert store.settings.pm_model == "opus"
        assert not store.is_locked

    @pytest.mark.asyncio
    async def test_unlock_without_pending_keeps_original(self):
        store = SettingsStore()
        original = store.settings.pm_model
        await store.lock()
        await store.unlock()
        assert store.settings.pm_model == original

    @pytest.mark.asyncio
    async def test_second_update_while_locked_replaces_pending(self):
        store = SettingsStore()
        await store.lock()
        await store.try_update(AppSettings(pm_model="first"))
        await store.try_update(AppSettings(pm_model="second"))
        await store.unlock()
        assert store.settings.pm_model == "second"

    @pytest.mark.asyncio
    async def test_settings_persist_to_file_and_reload(self):
        store = SettingsStore()
        await store.try_update(AppSettings(pm_model="custom-model"))
        assert SettingsStore._SETTINGS_FILE.exists()
        store2 = SettingsStore()
        assert store2.settings.pm_model == "custom-model"

    @pytest.mark.asyncio
    async def test_pending_settings_saved_on_unlock(self):
        store = SettingsStore()
        await store.lock()
        await store.try_update(AppSettings(pm_model="pending-model"))
        store2 = SettingsStore()
        assert store2.settings.pm_model != "pending-model"
        await store.unlock()
        store3 = SettingsStore()
        assert store3.settings.pm_model == "pending-model"

    @pytest.mark.asyncio
    async def test_loads_jsonc_with_comments(self, tmp_path):
        jsonc_file = tmp_path / "test.jsonc"
        jsonc_file.write_text(
            '{\n'
            '  // line comment\n'
            '  "pm_model": "from-jsonc", // inline comment\n'
            '  /* block\n'
            '     comment */\n'
            '  "opus_model": "opus"\n'
            '}\n'
        )
        store = SettingsStore(settings_file=jsonc_file)
        assert store.settings.pm_model == "from-jsonc"
        assert store.settings.opus_model == "opus"

    def test_strip_jsonc_preserves_urls_in_strings(self):
        text = '{"base_url": "https://example.com/api"}'
        result = SettingsStore._strip_jsonc_comments(text)
        assert json.loads(result)["base_url"] == "https://example.com/api"

    def test_strip_jsonc_preserves_comment_like_strings(self):
        text = '{"note": "use // for comments", "val": 1}'
        result = SettingsStore._strip_jsonc_comments(text)
        parsed = json.loads(result)
        assert parsed["note"] == "use // for comments"
        assert parsed["val"] == 1
