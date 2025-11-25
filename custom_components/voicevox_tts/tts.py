"""Support for the VOICEVOX TTS service."""

from __future__ import annotations
from typing import Any
import asyncio
import logging

import aiohttp
import voluptuous as vol

from homeassistant.components.tts import (
    TextToSpeechEntity,
    TtsAudioType,
)
from homeassistant import config_entries
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import CONF_HOST, CONF_PORT
import homeassistant.helpers.config_validation as cv

from .const import CONF_SPEAKER, SUPPORT_LANGUAGES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([VOICEVOXTTSEntity(hass, config_entry)])


class VOICEVOXTTSEntity(TextToSpeechEntity):
    def __init__(self, hass: HomeAssistant, config_entry: config_entries.ConfigEntry) -> None:
        self.hass = hass
        self.name = "VOICEVOX TTS"
        self._attr_name = f"VOICEVOX TTS"
        self._attr_unique_id = config_entry.entry_id
        self._config_entry = config_entry

    @property
    def default_language(self):
        return "ja-JP"

    @property
    def supported_languages(self):
        return SUPPORT_LANGUAGES

    async def async_get_tts_audio(self, message: str, language: str, options: dict[str, Any]) -> TtsAudioType:
        host = self._config_entry.data.get(CONF_HOST)
        port = self._config_entry.data.get(CONF_PORT)
        speaker = self._config_entry.options.get(CONF_SPEAKER)
        url = f"http://{host}:{port}"
        _LOGGER.debug(f"Start TTS. URL: {url}, Voice ID: {speaker}, Message: {message}")
        params = {"text": message, "speaker": speaker}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{url}/audio_query", params=params) as query_response:
                    query_data = await query_response.json()
                    query_data['volumeScale']=12.0  ## setting volume (default is 1.0)
                    query_data['speedScale']=1.1    ## setting speed (pitch) (default is 1.0)
                    _LOGGER.debug("Query fetched successfully. Start fetching audio data")

                async with session.post(f"{url}/synthesis", params={"speaker": speaker}, json=query_data) as synth_response:
                    data = await synth_response.read()
                    _LOGGER.debug("Audio data fetched successfully")
                    return ("wav", data)
        except aiohttp.ClientError as err:
            _LOGGER.error("Error occurred while requesting TTS audio: %s", err)

        return (None, None)
