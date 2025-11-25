"""The VOICEVOX TTS integration."""

from __future__ import annotations
import logging
import asyncio

import aiohttp
import voluptuous as vol
from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from .const import DOMAIN, CONF_SPEAKER, CONF_VOLUME, CONF_PITCH, CONF_SPEED, DEFAULT_VOLUME, DEFAULT_PITCH, DEFAULT_SPEED

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default='localhost'): str,
        vol.Required(CONF_PORT, default=50021): int,
    }
)


class VOICEVOXTTSConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """VOICEVOX TTS config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            existing_entries = self.hass.config_entries.async_entries(DOMAIN)
            for entry in existing_entries:
                if (entry.data.get(CONF_HOST) == user_input[CONF_HOST] and
                    entry.data.get(CONF_PORT) == user_input[CONF_PORT]):
                    _LOGGER.error("This configuration already exists")
                    return self.async_abort(reason="already_configured")
            try:
                url = f"http://{user_input[CONF_HOST]}:{user_input[CONF_PORT]}/speakers"
                _LOGGER.debug(f"Get voice information from the following URL: {url}")
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=5) as response:
                        response.raise_for_status()
                        speakers_data = await response.json()
                        self.speakers = [
                            {"id": style["id"], "name": f"{speaker["name"]}: {style["name"]}"}
                            for speaker in speakers_data
                            for style in speaker["styles"]
                        ]
                        self.config_data = user_input
                        return await self.async_step_speaker_selection()
            except asyncio.TimeoutError:
                _LOGGER.error("Connection timed out")
                errors["base"] = "timeout"
            except aiohttp.ClientError as err:
                _LOGGER.error(f"Connection error: {err}")
                errors["base"] = "error"
            except Exception as ex:
                _LOGGER.error(f"Unexpected error: {ex}")
                errors["base"] = "unknown"
        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)


    async def async_step_speaker_selection(self, user_input: dict[str, Any] | None = None):
        """Handle speaker selection step."""
        errors = {}
        speaker_options = {str(speaker["id"]): speaker["name"] for speaker in self.speakers}
        speaker_schema = vol.Schema(
            {
                vol.Required(CONF_SPEAKER): vol.In(speaker_options),
                vol.Optional(CONF_VOLUME, default=DEFAULT_VOLUME): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=20.0)),
                vol.Optional(CONF_PITCH, default=DEFAULT_PITCH): vol.All(vol.Coerce(float), vol.Range(min=-0.15, max=0.15)),
                vol.Optional(CONF_SPEED, default=DEFAULT_SPEED): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=3.0)),
            }
        )

        if user_input is not None:
            speaker_id = int(user_input[CONF_SPEAKER])
            volume = user_input[CONF_VOLUME]
            pitch = user_input[CONF_PITCH]
            speed = user_input[CONF_SPEED]
            _LOGGER.debug(f"Set voice to '{speaker_options.get(str(speaker_id))}'")
            return self.async_create_entry(
                title="VOICEVOX TTS",
                data=self.config_data,
                options={
                    CONF_SPEAKER: speaker_id,
                    CONF_VOLUME: volume,
                    CONF_PITCH: pitch,
                    CONF_SPEED: speed,
                }
            )

        return self.async_show_form(
            step_id="speaker_selection",
            data_schema=speaker_schema,
            errors=errors,
        )


    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        errors = {}
        entry = self._get_reconfigure_entry()
        data = user_input or entry.data
        if user_input is not None:
            try:
                url = f"http://{user_input[CONF_HOST]}:{user_input[CONF_PORT]}/"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=5) as response:
                        response.raise_for_status()
                        return self.async_update_reload_and_abort(
                            entry,
                            data_updates=user_input,
                        )
            except asyncio.TimeoutError:
                _LOGGER.error("Connection timed out")
                errors["base"] = "timeout"
            except aiohttp.ClientError as err:
                _LOGGER.error(f"Connection error: {err}")
                errors["base"] = "error"
            except Exception as ex:
                _LOGGER.error(f"Unexpected error: {ex}")
                errors["base"] = "unknown"

        RECONFIGURE_DATA_SCHEMA = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data.get(CONF_HOST)): str,
                vol.Required(CONF_PORT, default=entry.data.get(CONF_PORT)): int,
            }
        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=RECONFIGURE_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return VOICEVOXTTSOptionsFlowHandler(config_entry)


class VOICEVOXTTSOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for VOICEVOX TTS."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage the VOICEVOX TTS options."""
        errors = {}

        try:
            url = f"http://{self._config_entry.data.get(CONF_HOST)}:{self._config_entry.data.get(CONF_PORT)}/speakers"
            _LOGGER.debug(f"Get voice information from the following URL: {url}")
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as response:
                    response.raise_for_status()
                    speakers_data = await response.json()
                    speakers = [
                        {"id": style["id"], "name": f"{speaker['name']}: {style['name']}"}
                        for speaker in speakers_data
                        for style in speaker["styles"]
                    ]
                    speaker_options = {str(speaker["id"]): speaker["name"] for speaker in speakers}
        except Exception as ex:
            _LOGGER.error(f"Failed to fetch voice information: {ex}")
            errors["base"] = "speaker_fetch_failed"
            speaker_options = {}

        if user_input is not None:
            _LOGGER.debug(f"Change voice to '{speaker_options.get(str(user_input.get(CONF_SPEAKER)))}'")
            self.hass.config_entries.async_update_entry(self._config_entry, options=user_input)
            await self.hass.config_entries.async_reload(self._config_entry.entry_id)
            return self.async_abort(reason="options_updated")

        current_speaker_id = str(self._config_entry.options.get(CONF_SPEAKER))
        current_volume = self._config_entry.options.get(CONF_VOLUME, DEFAULT_VOLUME)
        current_pitch = self._config_entry.options.get(CONF_PITCH, DEFAULT_PITCH)
        current_speed = self._config_entry.options.get(CONF_SPEED, DEFAULT_SPEED)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SPEAKER, default=current_speaker_id): vol.In(speaker_options),
                    vol.Optional(CONF_VOLUME, default=current_volume): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=20.0)),
                    vol.Optional(CONF_PITCH, default=current_pitch): vol.All(vol.Coerce(float), vol.Range(min=-0.15, max=0.15)),
                    vol.Optional(CONF_SPEED, default=current_speed): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=3.0)),
                }
            ),
            errors=errors,
        )
