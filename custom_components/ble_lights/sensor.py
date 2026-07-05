"""Support for Generic BT notification sensors."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .generic_bt_api.device import GenericBTBleakError, GenericBTTimeoutError

from .const import DEFAULT_NOTIFY_UUID, DEFAULT_WRITE_UUID, DOMAIN, NOTIFICATION_REASSEMBLY_TIMEOUT_SECONDS, Schema
from .coordinator import GenericBTCoordinator
from .entity import GenericBTEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Generic BT notification sensors based on a config entry."""
    coordinator: GenericBTCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        GenericBTStateSensor(coordinator),
        GenericBTPreviousStateSensor(coordinator),
    ])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "sync_state",
        Schema.SYNC_STATE.value,
        "async_request_settings",
    )


class GenericBTStateSensor(GenericBTEntity, SensorEntity, RestoreEntity):
    """ON/OFF state derived from the decoded requestSettings response.

    The raw 40-byte payload is no longer exposed directly. native_value is
    ON/OFF from the decoded onOffSwitch field; every other decoded field
    (program, speed, colors, timer1, timer2, brightness, version, sync_mode,
    direction) is available as an attribute. Nothing here is populated until
    a *complete* reassembled packet has been decoded - see
    GenericBTDevice._complete_reassembly.
    """

    _attr_name = "state"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATE_ON, STATE_OFF]

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_state"
        self._attr_native_value: str | None = None
        self._device.set_state_callback(self._handle_state_update)

    async def async_added_to_hass(self) -> None:
        """Restore the last known value when the entity is added."""
        await super().async_added_to_hass()
        self._refresh_from_device()
        if self._attr_native_value is None:
            restored_state = await self.async_get_last_state()
            if restored_state is not None and restored_state.state not in (None, "unknown", "unavailable"):
                self._attr_native_value = restored_state.state
        self.async_write_ha_state()

    @callback
    def _handle_state_update(self) -> None:
        """Refresh the entity state when the device pushes a new complete notification."""
        self._refresh_from_device()
        self.async_write_ha_state()

    def _current_data(self) -> dict | None:
        """Parsed fields backing this entity. Overridden by the "previous" variant."""
        return self._device.last_notification_data

    def _refresh_from_device(self) -> None:
        data = self._current_data()
        if data is None:
            return
        self._attr_native_value = STATE_ON if data.get("is_on") else STATE_OFF

    @property
    def native_value(self) -> str | None:
        """Return ON/OFF derived from the decoded onOffSwitch field."""
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose every other decoded field from the settings packet."""
        data = self._current_data()
        if data is None:
            return None
        return {key: value for key, value in data.items() if key != "is_on"}

    async def async_request_settings(self, timeout: float | None = None) -> None:
        """Entity service handler: manually trigger a requestSettings round-trip.

        The response flows through the normal notification pipeline
        (device callback -> _handle_state_update -> async_write_ha_state),
        so there's nothing further to do here beyond kicking it off and
        surfacing a timeout if the device never replies.
        """
        try:
            result = await self._device.request_settings(
                DEFAULT_WRITE_UUID,
                notify_uuid=DEFAULT_NOTIFY_UUID,
                timeout=timeout if timeout is not None else NOTIFICATION_REASSEMBLY_TIMEOUT_SECONDS,
            )
        except (GenericBTBleakError, GenericBTTimeoutError) as exc:
            _LOGGER.warning(
                "request_settings service call for %s failed to connect: %s",
                self.entity_id,
                exc,
            )
            raise HomeAssistantError(
                f"Could not connect to {self.entity_id} to request settings"
            ) from exc

        if result is None:
            _LOGGER.warning(
                "request_settings service call for %s timed out waiting for a complete response",
                self.entity_id,
            )


class GenericBTPreviousStateSensor(GenericBTStateSensor):
    """Same as GenericBTStateSensor, but reflecting the previous distinct reading."""

    _attr_name = "previous state"

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_previous_state"

    def _current_data(self) -> dict | None:
        return self._device.previous_notification_data