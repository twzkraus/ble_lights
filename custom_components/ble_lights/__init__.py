"""Support for generic bluetooth devices."""

import contextlib
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DEFAULT_NOTIFY_UUID, DOMAIN
from .coordinator import GenericBTCoordinator
from .generic_bt_api.device import GenericBTDevice


_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.NUMBER, Platform.SWITCH, Platform.SENSOR, Platform.LIGHT, Platform.SELECT]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Generic BT from a config entry."""
    assert entry.unique_id is not None
    hass.data.setdefault(DOMAIN, {})
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(f"Could not find Generic BT Device with address {address}")
    device = GenericBTDevice(ble_device)
    coordinator = hass.data[DOMAIN][entry.entry_id] = GenericBTCoordinator(
        hass, _LOGGER, ble_device, device, entry.title, entry.unique_id, True
    )
    entry.async_on_unload(coordinator.async_start())

    if not await coordinator.async_wait_ready():
        raise ConfigEntryNotReady(f"{address} is not advertising state")

    # This used to be wrapped in contextlib.suppress(Exception),
    # which silently discarded any failure
    try:
        await device.subscribe_to_notify(DEFAULT_NOTIFY_UUID)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.warning(
            "Failed to subscribe to notifications for %s on UUID %s - "
            "notification-based sensors will not update until this succeeds",
            address,
            DEFAULT_NOTIFY_UUID,
            exc_info=True,
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: GenericBTCoordinator = hass.data[DOMAIN][entry.entry_id]
        with contextlib.suppress(Exception):
            await coordinator.device.stop()
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.config_entries.async_entries(DOMAIN):
            hass.data.pop(DOMAIN)

    return unload_ok
