"""The VOICEVOX TTS integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
import aiohttp
import asyncio

PLATFORMS: list[Platform] = [Platform.TTS]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VOICEVOX TTS from a config entry."""
    host = entry.data.get("host")
    port = entry.data.get("port")
    url = f"http://{host}:{port}/"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as response:
                response.raise_for_status()
                await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
                entry.async_on_unload(entry.add_update_listener(async_reload_entry))
                return True
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise ConfigEntryNotReady(f"Failed to connect to VOICEVOX Engine: {e}")

    return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when it is updated."""
    await hass.config_entries.async_reload(entry.entry_id)