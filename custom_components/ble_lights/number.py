"""Support for Generic BT idle disconnect timeout number."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_WRITE_UUID, DOMAIN
from .coordinator import GenericBTCoordinator
from .entity import GenericBTEntity
from .generic_bt_api.device import DEFAULT_IDLE_DISCONNECT_SECONDS


_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the number entities."""
    coordinator: GenericBTCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([GenericBTIdleDisconnectNumber(coordinator), GenericBTSpeedNumber(coordinator), GenericBTBrightnessNumber(coordinator)])


class GenericBTIdleDisconnectNumber(GenericBTEntity, NumberEntity, RestoreEntity):
    """Expose the idle disconnect timeout as a configurable HA number entity."""

    _attr_name = "Auto disconnect timeout"
    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 3600
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "s"

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_idle_disconnect_timeout"
        self._attr_native_value = float(DEFAULT_IDLE_DISCONNECT_SECONDS)

    async def async_added_to_hass(self) -> None:
        """Restore the last configured timeout and apply it to the device."""
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except ValueError:
                _LOGGER.debug("Unable to restore idle disconnect timeout from %s", last_state.state)

        self._device.set_idle_disconnect_seconds(float(self._attr_native_value))
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the currently configured idle disconnect timeout."""
        return self._device.idle_disconnect_seconds

    async def async_set_native_value(self, value: float) -> None:
        """Update the configured idle disconnect timeout."""
        self._device.set_idle_disconnect_seconds(float(value))
        self._attr_native_value = float(value)
        self.async_write_ha_state()


class GenericBTSpeedNumber(GenericBTEntity, NumberEntity, RestoreEntity):
    """Expose the speed as a configurable HA number entity."""

    _attr_name = "Effect speed"
    _attr_icon = "mdi:fast-forward"
    _attr_native_min_value = 0
    _attr_native_max_value = 255
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_effect_speed"
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        """Restore the last configured speed and apply it to the device."""
        await super().async_added_to_hass()
        self._refresh_from_device()

        if self._device.last_notification_data is None:
            restored_state = await self.async_get_last_state()
            if restored_state is not None and restored_state.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                try:
                    self._attr_native_value = int(float(restored_state.state))
                except ValueError:
                    pass
        self.async_write_ha_state()

    def _refresh_from_device(self) -> None:
        data = self._device.last_notification_data
        if data is None:
            return
        if (speed := data.get("speed")) is not None:
            self._attr_native_value = speed

    async def async_set_native_value(self, value: float) -> None:
        """Update the configured effect speed."""
        await self._async_apply_speed(int(value))
        self.async_write_ha_state()
        await self._device.request_settings(DEFAULT_WRITE_UUID)

# ---------------------------- Private Helpers --------------------------------
    async def _async_apply_speed(self, speed: int) -> None:
        """Send a speed level (0-255) to the light."""
        await self._device.set_speed(DEFAULT_WRITE_UUID, speed)
        self._attr_native_value = speed


class GenericBTBrightnessNumber(GenericBTEntity, NumberEntity, RestoreEntity):
    """Expose the brightness as a configurable HA number entity."""

    _attr_name = "Brightness"
    _attr_icon = "mdi:brightness-5"
    _attr_native_min_value = 0
    _attr_native_max_value = 255
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_brightness"
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        """Restore the last configured brightness and apply it to the device."""
        await super().async_added_to_hass()
        self._refresh_from_device()

        if self._device.last_notification_data is None:
            restored_state = await self.async_get_last_state()
            if restored_state is not None and restored_state.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                try:
                    self._attr_native_value = int(float(restored_state.state))
                except ValueError:
                    pass
        self.async_write_ha_state()

    def _refresh_from_device(self) -> None:
        data = self._device.last_notification_data
        if data is None:
            return
        if (brightness := data.get("brightness")) is not None:
            self._attr_native_value = brightness

    async def async_set_native_value(self, value: float) -> None:
        """Update the configured brightness."""
        await self._async_apply_brightness(int(value))
        self.async_write_ha_state()
        await self._device.request_settings(DEFAULT_WRITE_UUID)

# ---------------------------- Private Helpers --------------------------------
    async def _async_apply_brightness(self, brightness: int) -> None:
        """Send a brightness level (0-255) to the light."""
        await self._device.set_brightness(DEFAULT_WRITE_UUID, brightness)
        self._attr_native_value = brightness

