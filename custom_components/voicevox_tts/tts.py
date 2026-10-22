"""Support for the VOICEVOX TTS service."""

from __future__ import annotations
from collections.abc import AsyncGenerator
from typing import Any
import asyncio
import logging

import aiohttp

from homeassistant.components.tts import (
    TextToSpeechEntity,
    TtsAudioType,
    TTSAudioRequest,
    TTSAudioResponse,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import CONF_HOST, CONF_PORT

from .const import (
    CONF_SPEAKER,
    SUPPORT_LANGUAGES,
    CONF_VOLUME,
    CONF_PITCH,
    CONF_SPEED,
    CONF_USE_STREAMING,
    DEFAULT_VOLUME,
    DEFAULT_PITCH,
    DEFAULT_SPEED,
    DEFAULT_USE_STREAMING,
)

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
        self.name = config_entry.title
        self._attr_name = config_entry.title
        self._attr_unique_id = config_entry.entry_id
        self._config_entry = config_entry
        self.speaker = config_entry.options.get(CONF_SPEAKER)
        self.volume = config_entry.options.get(CONF_VOLUME, DEFAULT_VOLUME)
        self.pitch = config_entry.options.get(CONF_PITCH, DEFAULT_PITCH)
        self.speed = config_entry.options.get(CONF_SPEED, DEFAULT_SPEED)
        self.use_streaming = config_entry.options.get(CONF_USE_STREAMING, DEFAULT_USE_STREAMING)

    @property
    def default_language(self):
        return "ja-JP"

    @property
    def supported_languages(self):
        return SUPPORT_LANGUAGES

    @property
    def supported_options(self) -> list[str]:
        """Return list of supported options."""
        return [CONF_SPEAKER, CONF_VOLUME, CONF_PITCH, CONF_SPEED]

    async def async_get_tts_audio(self, message: str, language: str, options: dict[str, Any]) -> TtsAudioType:
        try:
            data = b"".join([chunk async for chunk in self._async_generate_audio(message, options)])
            return ("wav", data) if data else (None, None)
        except HomeAssistantError:
            return (None, None)

    async def async_stream_tts_audio(self, request: TTSAudioRequest) -> TTSAudioResponse:
        """Stream generated audio to Home Assistant."""
        message = "".join([chunk async for chunk in request.message_gen])
        return TTSAudioResponse("wav", self._async_generate_audio(message, request.options))

    async def _async_generate_audio(self, message: str, options: dict[str, Any]) -> AsyncGenerator[bytes]:
        host = self._config_entry.data.get(CONF_HOST)
        port = self._config_entry.data.get(CONF_PORT)
        url = f"http://{host}:{port}"

        speaker = options.get(CONF_SPEAKER, self.speaker)
        volume = options.get(CONF_VOLUME, self.volume)
        pitch = options.get(CONF_PITCH, self.pitch)
        speed = options.get(CONF_SPEED, self.speed)

        _LOGGER.debug(
            "Start TTS. URL: %s, Voice ID: %s, Volume: %s, Pitch: %s, Speed: %s, Message: %s",
            url, speaker, volume, pitch, speed, message,
        )
        params = {"text": message, "speaker": speaker}
        session = async_get_clientsession(self.hass)
        timeout = aiohttp.ClientTimeout(total=180, sock_connect=10, sock_read=60)
        try:
            async with session.post(f"{url}/audio_query", params=params, timeout=timeout) as response:
                response.raise_for_status()
                query_data = await response.json()
            query_data['volumeScale'] = volume
            query_data['pitchScale'] = pitch
            query_data['speedScale'] = speed
            _LOGGER.debug("Query fetched successfully. Start fetching audio data")

            if self.use_streaming:
                audio_started = False
                try:
                    async with session.post(
                        f"{url}/streaming_synthesis", params={"speaker": speaker},
                        json=query_data, timeout=timeout,
                    ) as response:
                        response.raise_for_status()
                        _LOGGER.debug("Generating audio using the streaming API")
                        async for chunk in response.content.iter_any():
                            audio_started = True
                            yield chunk
                    if audio_started:
                        _LOGGER.debug("Audio data fetched successfully")
                        return
                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    # 配信開始後の再生成は、同じ音声の重複再生につながります。
                    if audio_started:
                        raise
                    _LOGGER.debug("Streaming API unavailable. Falling back to the synthesis API: %s", err)

            async with session.post(
                f"{url}/synthesis", params={"speaker": speaker},
                json=query_data, timeout=timeout,
            ) as response:
                response.raise_for_status()
                data = await response.read()
                if not data:
                    raise HomeAssistantError("VOICEVOXエンジンから音声が返されませんでした")
                _LOGGER.debug("Audio generated using the synthesis API")
                _LOGGER.debug("Audio data fetched successfully")
                yield data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.error("Error occurred while requesting TTS audio: %s", err)
            raise HomeAssistantError("VOICEVOXエンジンとの音声通信に失敗しました") from err
