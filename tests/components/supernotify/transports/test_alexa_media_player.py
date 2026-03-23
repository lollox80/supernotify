"""Tests for the Alexa Media Player transport volume management.

Covers:
- _estimate_tts_duration (SSML stripping, pause chars, char_weight param)
- _to_bool helper (string booleans, frozenset falsy values)
- _snapshot_states (normal, playing, volume_level=None, missing entity)
- _pre_announce (media_stop ordering, pause/stop logic, idle no-op)
- _post_announce (restore, skip restore, resume music, no resume when idle,
                  no media_stop beep in post-announce)
- deliver() (no targets, no volume, volume set+restore, volume not forwarded,
             restore_volume=False, volume_fallback, exception no-block,
             music pause+resume, template resolution, wait_for_tts,
             tts_char_speed, string boolean "false" handling)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.supernotify.transports.alexa_media_player import (
    _BASE_DURATION,
    _CHAR_WEIGHT,
    _MUSIC_RESUME_DELAY,
    _PAUSE_WEIGHT,
    PAUSE_CHARS,
    AlexaMediaPlayerTransport,
    _estimate_tts_duration,
    _to_bool,
)


def _make_transport(states=None):
    def _get_state(entity_id):
        if not states or entity_id not in states:
            return None
        raw = states[entity_id]
        state_obj = MagicMock()
        state_obj.state = raw.get("state", "idle")
        state_obj.attributes = {k: v for k, v in raw.items() if k != "state"}
        return state_obj

    hass_api = MagicMock()
    hass_api.call_service = AsyncMock(return_value=None)
    hass_api.get_state = MagicMock(side_effect=_get_state)
    template_mock = MagicMock()
    template_mock.async_render = MagicMock(return_value="0.7")
    hass_api.template = MagicMock(return_value=template_mock)

    transport = AlexaMediaPlayerTransport.__new__(AlexaMediaPlayerTransport)
    transport.hass_api = hass_api
    transport.call_action = AsyncMock(return_value=True)
    return transport


def _make_envelope(message, data=None, entity_ids=None, condition_variables=None):
    envelope = MagicMock()
    envelope.message = message
    envelope.data = data if data is not None else {}
    envelope.target = MagicMock()
    envelope.target.entity_ids = entity_ids or ["media_player.ufficio"]
    envelope.condition_variables = condition_variables
    return envelope


def _service_names(transport):
    return [c.args[1] for c in transport.hass_api.call_service.call_args_list]


class TestToBool:
    def test_true_bool(self):
        assert _to_bool(True, default=False) is True

    def test_false_bool(self):
        assert _to_bool(False, default=True) is False

    def test_string_false_lowercase(self):
        assert _to_bool("false", default=True) is False

    def test_string_False_capitalized(self):
        assert _to_bool("False", default=True) is False

    def test_string_zero(self):
        assert _to_bool("0", default=True) is False

    def test_string_no(self):
        assert _to_bool("no", default=True) is False

    def test_string_off(self):
        assert _to_bool("off", default=True) is False

    def test_string_true(self):
        assert _to_bool("true", default=False) is True

    def test_string_yes(self):
        assert _to_bool("yes", default=False) is True

    def test_integer_one(self):
        assert _to_bool(1, default=False) is True

    def test_integer_zero(self):
        assert _to_bool(0, default=True) is False

    def test_empty_string(self):
        assert _to_bool("", default=True) is False


class TestEstimateTtsDuration:
    def test_empty_message(self):
        assert _estimate_tts_duration("") == pytest.approx(_BASE_DURATION)

    def test_plain_text(self):
        msg = "Ciao"
        expected = _BASE_DURATION + 4 * _CHAR_WEIGHT
        assert _estimate_tts_duration(msg) == pytest.approx(expected)

    def test_pause_chars_counted(self):
        msg = "Hello. World! How, are you?"
        plain_len = len(msg)
        pause_count = sum(msg.count(p) for p in PAUSE_CHARS)
        expected = _BASE_DURATION + pause_count * _PAUSE_WEIGHT + plain_len * _CHAR_WEIGHT
        assert _estimate_tts_duration(msg) == pytest.approx(expected)

    def test_ssml_stripped(self):
        with_ssml = '<amazon:effect name="whispered">Hello world</amazon:effect>'
        plain = "Hello world"
        assert _estimate_tts_duration(with_ssml) == pytest.approx(_estimate_tts_duration(plain))

    def test_custom_char_weight(self):
        msg = "abc"
        custom = 0.10
        expected = _BASE_DURATION + 3 * custom
        assert _estimate_tts_duration(msg, char_weight=custom) == pytest.approx(expected)


class TestSnapshotStates:
    @pytest.mark.asyncio
    async def test_normal_volume(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.6}})
        states = await t._snapshot_states(["media_player.ufficio"], 0.5)
        assert states["media_player.ufficio"]["volume"] == pytest.approx(0.6)
        assert states["media_player.ufficio"]["playing"] is False

    @pytest.mark.asyncio
    async def test_playing_device(self):
        t = _make_transport({"media_player.sala": {"state": "playing", "volume_level": 0.3}})
        states = await t._snapshot_states(["media_player.sala"], 0.5)
        assert states["media_player.sala"]["playing"] is True

    @pytest.mark.asyncio
    async def test_volume_none_uses_fallback(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": None}})
        states = await t._snapshot_states(["media_player.ufficio"], 0.42)
        assert states["media_player.ufficio"]["volume"] == pytest.approx(0.42)

    @pytest.mark.asyncio
    async def test_missing_entity_skipped(self):
        t = _make_transport()
        states = await t._snapshot_states(["media_player.ghost"], 0.5)
        assert "media_player.ghost" not in states


class TestPreAnnounce:
    @pytest.mark.asyncio
    async def test_idle_device_no_stop_no_pause(self):
        """media_stop must NOT be called on idle devices — triggers a beep."""
        t = _make_transport()
        states = {"media_player.ufficio": {"volume": 0.3, "playing": False}}
        await t._pre_announce(states, 0.9, pause_music=True)
        names = _service_names(t)
        assert "media_stop" not in names
        assert "media_pause" not in names
        assert "volume_set" in names

    @pytest.mark.asyncio
    async def test_playing_pause_music_true_uses_media_pause_only(self):
        """pause_music=True + playing: media_pause only, no media_stop."""
        t = _make_transport()
        states = {"media_player.sala": {"volume": 0.5, "playing": True}}
        await t._pre_announce(states, 0.8, pause_music=True)
        names = _service_names(t)
        assert "media_pause" in names
        assert "media_stop" not in names

    @pytest.mark.asyncio
    async def test_playing_pause_music_false_uses_media_stop(self):
        """pause_music=False + playing: media_stop only."""
        t = _make_transport()
        states = {"media_player.sala": {"volume": 0.5, "playing": True}}
        await t._pre_announce(states, 0.8, pause_music=False)
        names = _service_names(t)
        assert "media_stop" in names
        assert "media_pause" not in names

    @pytest.mark.asyncio
    async def test_volume_set_on_all_devices(self):
        t = _make_transport()
        states = {
            "media_player.ufficio": {"volume": 0.3, "playing": False},
            "media_player.sala": {"volume": 0.5, "playing": False},
        }
        await t._pre_announce(states, 0.8, pause_music=True)
        vol_calls = [c for c in t.hass_api.call_service.call_args_list if c.args[1] == "volume_set"]
        assert len(vol_calls) == 2
        for vc in vol_calls:
            assert vc.args[2]["volume_level"] == pytest.approx(0.8)


class TestPostAnnounce:
    @pytest.mark.asyncio
    async def test_no_media_stop_in_post_announce(self):
        """media_stop must NOT be called after TTS — Alexa is already idle."""
        t = _make_transport()
        states = {"media_player.ufficio": {"volume": 0.35, "playing": False}}
        await t._post_announce(states, restore_volume=True, pause_music=True)
        assert "media_stop" not in _service_names(t)

    @pytest.mark.asyncio
    async def test_restores_volume(self):
        t = _make_transport()
        states = {"media_player.ufficio": {"volume": 0.35, "playing": False}}
        await t._post_announce(states, restore_volume=True, pause_music=True)
        vol_calls = [c for c in t.hass_api.call_service.call_args_list if c.args[1] == "volume_set"]
        assert len(vol_calls) == 1
        assert vol_calls[0].args[2]["volume_level"] == pytest.approx(0.35)

    @pytest.mark.asyncio
    async def test_skips_restore_when_disabled(self):
        t = _make_transport()
        states = {"media_player.ufficio": {"volume": 0.35, "playing": False}}
        await t._post_announce(states, restore_volume=False, pause_music=True)
        vol_calls = [c for c in t.hass_api.call_service.call_args_list if c.args[1] == "volume_set"]
        assert len(vol_calls) == 0

    @pytest.mark.asyncio
    async def test_resumes_music_after_delay(self):
        t = _make_transport()
        states = {"media_player.sala": {"volume": 0.5, "playing": True}}
        with patch(
            "custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            await t._post_announce(states, restore_volume=True, pause_music=True)
            mock_sleep.assert_awaited_once_with(_MUSIC_RESUME_DELAY)
        assert "media_play" in _service_names(t)

    @pytest.mark.asyncio
    async def test_no_resume_when_was_not_playing(self):
        t = _make_transport()
        states = {"media_player.ufficio": {"volume": 0.5, "playing": False}}
        with patch(
            "custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            await t._post_announce(states, restore_volume=True, pause_music=True)
            mock_sleep.assert_not_awaited()
        assert "media_play" not in _service_names(t)


class TestDeliver:
    @pytest.mark.asyncio
    async def test_no_targets_returns_false(self):
        t = _make_transport()
        envelope = _make_envelope("Test", entity_ids=[])
        assert await t.deliver(envelope) is False

    @pytest.mark.asyncio
    async def test_no_volume_no_volume_set(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.5}})
        envelope = _make_envelope("Test", data={})
        result = await t.deliver(envelope)
        assert result is True
        assert "volume_set" not in _service_names(t)

    @pytest.mark.asyncio
    async def test_volume_set_and_restored(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.4}})
        envelope = _make_envelope("Test", data={"volume": 0.9, "type": "announce"})
        with patch("custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock):
            result = await t.deliver(envelope)
        assert result is True
        vol_calls = [c for c in t.hass_api.call_service.call_args_list if c.args[1] == "volume_set"]
        levels = [c.args[2]["volume_level"] for c in vol_calls]
        assert pytest.approx(0.9) in levels
        assert pytest.approx(0.4) in levels

    @pytest.mark.asyncio
    async def test_volume_not_forwarded_to_alexa(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.5}})
        envelope = _make_envelope("Test", data={"volume": 0.8, "type": "announce"})
        with patch("custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock):
            await t.deliver(envelope)
        action_data = t.call_action.call_args.kwargs.get("action_data", {})
        assert "volume" not in action_data.get("data", {})
        assert "volume_template" not in action_data.get("data", {})

    @pytest.mark.asyncio
    async def test_restore_volume_false(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.4}})
        envelope = _make_envelope("Test", data={"volume": 0.9, "restore_volume": False})
        with patch("custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock):
            await t.deliver(envelope)
        vol_calls = [c for c in t.hass_api.call_service.call_args_list if c.args[1] == "volume_set"]
        assert len(vol_calls) == 1

    @pytest.mark.asyncio
    async def test_restore_volume_string_false(self):
        """restore_volume: 'false' (YAML string) must be treated as False."""
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.4}})
        envelope = _make_envelope("Test", data={"volume": 0.9, "restore_volume": "false"})
        with patch("custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock):
            await t.deliver(envelope)
        vol_calls = [c for c in t.hass_api.call_service.call_args_list if c.args[1] == "volume_set"]
        assert len(vol_calls) == 1

    @pytest.mark.asyncio
    async def test_pause_music_string_false(self):
        """pause_music: 'false' (YAML string) must be treated as False."""
        t = _make_transport({"media_player.sala": {"state": "playing", "volume_level": 0.5}})
        envelope = _make_envelope("Test", data={"volume": 0.8, "pause_music": "false"}, entity_ids=["media_player.sala"])
        with patch("custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock):
            await t.deliver(envelope)
        assert "media_pause" not in _service_names(t)

    @pytest.mark.asyncio
    async def test_volume_fallback_restored(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": None}})
        envelope = _make_envelope("Test", data={"volume": 0.9, "volume_fallback": 0.55})
        with patch("custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock):
            await t.deliver(envelope)
        vol_calls = [c for c in t.hass_api.call_service.call_args_list if c.args[1] == "volume_set"]
        levels = [c.args[2]["volume_level"] for c in vol_calls]
        assert pytest.approx(0.55) in levels

    @pytest.mark.asyncio
    async def test_service_exception_does_not_block(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.5}})
        t.hass_api.call_service = AsyncMock(side_effect=Exception("offline"))
        envelope = _make_envelope("Test", data={"volume": 0.8})
        with patch("custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock):
            result = await t.deliver(envelope)
        assert result is True

    @pytest.mark.asyncio
    async def test_music_paused_and_resumed(self):
        t = _make_transport({"media_player.sala": {"state": "playing", "volume_level": 0.5}})
        envelope = _make_envelope("Test", data={"volume": 0.8}, entity_ids=["media_player.sala"])
        with patch("custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock):
            await t.deliver(envelope)
        names = _service_names(t)
        assert "media_pause" in names
        assert "media_play" in names

    @pytest.mark.asyncio
    async def test_volume_template_resolved(self):
        """Jinja2 volume template must be resolved to float before volume_set."""
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.3}})
        t.hass_api.template.return_value.async_render.return_value = "0.65"
        envelope = _make_envelope(
            "Test", data={"volume": "{{ (states('input_number.notifier_morning_volume') | float(40)) / 100 }}"}
        )
        with patch("custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock):
            await t.deliver(envelope)
        vol_calls = [c for c in t.hass_api.call_service.call_args_list if c.args[1] == "volume_set"]
        levels = [c.args[2]["volume_level"] for c in vol_calls]
        assert pytest.approx(0.65) in levels

    @pytest.mark.asyncio
    async def test_invalid_volume_template_ignored(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.3}})
        t.hass_api.template.return_value.async_render.side_effect = Exception("template error")
        envelope = _make_envelope("Test", data={"volume": "{{ invalid }}"})
        with patch("custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock):
            result = await t.deliver(envelope)
        assert result is True
        assert "volume_set" not in _service_names(t)

    @pytest.mark.asyncio
    async def test_wait_for_tts_blocks(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.5}})
        envelope = _make_envelope("Ciao", data={"wait_for_tts": True})
        with patch(
            "custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            await t.deliver(envelope)
            mock_sleep.assert_awaited_once()
            assert mock_sleep.call_args.args[0] >= _BASE_DURATION

    @pytest.mark.asyncio
    async def test_wait_for_tts_false_no_sleep_without_volume(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.5}})
        envelope = _make_envelope("Ciao", data={})
        with patch(
            "custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            await t.deliver(envelope)
            mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tts_char_speed_affects_duration(self):
        t = _make_transport({"media_player.ufficio": {"state": "idle", "volume_level": 0.5}})
        msg = "abc"
        fast_speed = 0.001
        envelope = _make_envelope(msg, data={"wait_for_tts": True, "tts_char_speed": fast_speed})
        with patch(
            "custom_components.supernotify.transports.alexa_media_player.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            await t.deliver(envelope)
            duration = mock_sleep.call_args.args[0]
        expected = _BASE_DURATION + 3 * fast_speed
        assert duration == pytest.approx(expected, rel=0.01)
