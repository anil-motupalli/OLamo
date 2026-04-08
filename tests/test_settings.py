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
    async def test_is_locked_while_any_run_active(self):
        """is_locked stays True until every lock() is matched by an unlock()."""
        store = SettingsStore()
        await store.lock()
        await store.lock()
        assert store.is_locked
        await store.unlock()
        assert store.is_locked          # still one active run
        await store.unlock()
        assert not store.is_locked      # now truly idle

    @pytest.mark.asyncio
    async def test_pending_not_applied_until_all_unlocked(self):
        """Pending settings must not be applied while any run still holds a lock."""
        store = SettingsStore()
        await store.lock()   # run A
        await store.lock()   # run B
        await store.try_update(AppSettings(pm_model="concurrent-update"))
        await store.unlock()  # run A finishes
        # run B still active — settings should NOT have been applied yet
        assert store.settings.pm_model != "concurrent-update"
        await store.unlock()  # run B finishes
        assert store.settings.pm_model == "concurrent-update"

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


class TestApiKeyRedirect:
    """Plain API keys submitted via the UI must be saved to .env, not to the settings file."""

    @pytest.fixture()
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(SettingsStore, "_SETTINGS_FILE", tmp_path / "settings.json")
        return SettingsStore(), tmp_path

    def _make_settings_with_key(self, api_key: str) -> AppSettings:
        from app.models import AgentEngineConfig, ModelConfig
        return AppSettings(agent_configs={
            "lead-developer": AgentEngineConfig(
                model_config=ModelConfig(api_key=api_key)
            )
        })

    @pytest.mark.asyncio
    async def test_plain_key_is_written_to_dotenv_not_settings_file(self, store):
        s, tmp_path = store
        await s.try_update(self._make_settings_with_key("sk-plain-secret"))

        settings_data = json.loads((tmp_path / "settings.json").read_text())
        saved_key = settings_data["agent_configs"]["lead-developer"]["model_config"]["api_key"]
        assert saved_key.startswith("env:"), f"Expected env: ref, got: {saved_key!r}"
        assert "sk-plain-secret" not in (tmp_path / "settings.json").read_text()

    @pytest.mark.asyncio
    async def test_plain_key_value_appears_in_dotenv_file(self, store):
        s, tmp_path = store
        await s.try_update(self._make_settings_with_key("sk-plain-secret"))

        env_file = tmp_path / ".env"
        assert env_file.exists(), ".env file was not created"
        assert "sk-plain-secret" in env_file.read_text()

    @pytest.mark.asyncio
    async def test_existing_env_ref_is_reused_on_update(self, store):
        """When the current api_key is env:MY_VAR, a new plain key updates MY_VAR in .env."""
        s, tmp_path = store
        # Load with an existing env: reference
        from app.models import AgentEngineConfig, ModelConfig
        initial = AppSettings(agent_configs={
            "lead-developer": AgentEngineConfig(
                model_config=ModelConfig(api_key="env:MY_CUSTOM_VAR")
            )
        })
        await s.try_update(initial)

        # Now update with a plain key — should reuse MY_CUSTOM_VAR
        await s.try_update(self._make_settings_with_key("sk-new-value"))

        settings_data = json.loads((tmp_path / "settings.json").read_text())
        saved_key = settings_data["agent_configs"]["lead-developer"]["model_config"]["api_key"]
        assert saved_key == "env:MY_CUSTOM_VAR"
        assert "MY_CUSTOM_VAR" in (tmp_path / ".env").read_text()
        assert "sk-new-value" in (tmp_path / ".env").read_text()

    @pytest.mark.asyncio
    async def test_env_ref_passthrough_unchanged(self, store):
        """An api_key already starting with env: is written as-is to the settings file."""
        s, tmp_path = store
        await s.try_update(self._make_settings_with_key("env:SOME_VAR"))

        settings_data = json.loads((tmp_path / "settings.json").read_text())
        saved_key = settings_data["agent_configs"]["lead-developer"]["model_config"]["api_key"]
        assert saved_key == "env:SOME_VAR"

    @pytest.mark.asyncio
    async def test_plain_key_sets_os_environ_immediately(self, store, monkeypatch):
        """After saving a plain key the env var is available in os.environ right away."""
        import os
        s, tmp_path = store
        await s.try_update(self._make_settings_with_key("sk-live-key"))

        # Find what var name was assigned
        settings_data = json.loads((tmp_path / "settings.json").read_text())
        ref = settings_data["agent_configs"]["lead-developer"]["model_config"]["api_key"]
        var_name = ref[4:]  # strip "env:"
        assert os.environ.get(var_name) == "sk-live-key"

