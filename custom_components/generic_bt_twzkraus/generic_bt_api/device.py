"""generic bt device"""

from uuid import UUID
from typing import Callable, Optional
import asyncio
import logging
import struct
from contextlib import AsyncExitStack, suppress

from bleak import BleakClient
from bleak.exc import BleakError

try:
    from bleak_retry_connector import establish_connection
except ImportError:  # pragma: no cover - optional dependency in tests
    establish_connection = None

_LOGGER = logging.getLogger(__name__)

# Default seconds of inactivity before we auto-disconnect.
# 0 / None disables idle-disconnect entirely.
DEFAULT_IDLE_DISCONNECT_SECONDS = 30

# requestSettings response is a fixed 40-byte binary struct, NOT text.
# Layout (little-endian, all unsigned bytes unless noted):
#   1  program
#   1  speed
#   18 colors[6] (hsv triples, 3 bytes each)
#   1  onOffSwitch
#   7  timer1Settings
#   7  timer2Settings
#   1  brightness
#   1  version
#   1  syncMode
#   1  direction
#   1  unused
SETTINGS_PACKET_LENGTH = 40
_TIMER_STRUCT = struct.Struct("<7B")  # timerOnOff, startSunset, startHour, startMinute, endSunrise, endHour, endMinute


def _parse_timer(raw: bytes) -> dict:
    (timer_on_off, start_sunset, start_hour, start_minute,
     end_sunrise, end_hour, end_minute) = _TIMER_STRUCT.unpack(raw)
    return {
        "timer_on_off": timer_on_off,
        "start_sunset": start_sunset,
        "start_hour": start_hour,
        "start_minute": start_minute,
        "end_sunrise": end_sunrise,
        "end_hour": end_hour,
        "end_minute": end_minute,
    }


def parse_settings_packet(raw_bytes: bytes) -> dict:
    """Parse the 40-byte requestSettings response into structured fields.

    Raises ValueError if raw_bytes is shorter than the expected packet size.
    """
    if len(raw_bytes) < SETTINGS_PACKET_LENGTH:
        raise ValueError(
            f"Expected at least {SETTINGS_PACKET_LENGTH} bytes, got {len(raw_bytes)}: {raw_bytes.hex()}"
        )

    program = raw_bytes[0]
    speed = raw_bytes[1]

    colors = []
    offset = 2
    for _ in range(6):
        hue, saturation, value = raw_bytes[offset:offset + 3]
        colors.append({"hue": hue, "saturation": saturation, "value": value})
        offset += 3
    # offset == 20 here

    on_off_switch = raw_bytes[offset]
    offset += 1

    timer1 = _parse_timer(raw_bytes[offset:offset + 7])
    offset += 7
    timer2 = _parse_timer(raw_bytes[offset:offset + 7])
    offset += 7

    brightness = raw_bytes[offset]
    offset += 1
    version = raw_bytes[offset]
    offset += 1
    sync_mode = raw_bytes[offset]
    offset += 1
    direction = raw_bytes[offset]
    offset += 1

    return {
        "program": program,
        "speed": speed,
        "colors": colors,
        "on_off_switch": bool(on_off_switch),
        "timer1": timer1,
        "timer2": timer2,
        "brightness": brightness,
        "version": version,
        "sync_mode": sync_mode,
        "direction": direction,
    }


class GenericBTBleakError(Exception):
    """Wrap bleak connection errors."""


class GenericBTTimeoutError(Exception):
    """Wrap bleak timeout errors."""


class GenericBTDevice:
    """Generic BT Device Class"""

    def __init__(self, ble_device, idle_disconnect_seconds: Optional[float] = DEFAULT_IDLE_DISCONNECT_SECONDS):
        self._ble_device = ble_device
        self._client: Optional[BleakClient] = None
        self._client_stack = AsyncExitStack()
        self._lock = asyncio.Lock()
        self._client_uses_context_manager = False

        # idle-disconnect bookkeeping
        self._idle_disconnect_seconds = idle_disconnect_seconds
        self._idle_disconnect_enabled = bool(idle_disconnect_seconds)
        self._idle_timer_handle: Optional[asyncio.TimerHandle] = None

        # entities (binary_sensor, switch, ...) register here to be notified
        # whenever connection state changes, regardless of what triggered it
        # (manual switch, idle timeout, a write/read service call, unload).
        self._listeners: list[Callable[[], None]] = []
        self._notify_uuid: Optional[str] = None
        self._read_uuid: Optional[str] = None
        self._notification_callback: Optional[Callable[[str], None]] = None
        # Whether start_notify has actually been armed on the *current*
        # self._client. This is distinct from self._notify_uuid, which is
        # the desired/target subscription and survives disconnects - it's
        # what get_client() uses to auto-resubscribe on every (re)connect.
        self._notify_active: bool = False
        self._state_callbacks: list[Callable[[], None]] = []
        self.last_notification_value: Optional[str] = None
        self._previous_notification_value: Optional[str] = None
        self.last_notification_data: Optional[dict] = None
        self._previous_notification_data: Optional[dict] = None

    def add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback to be invoked whenever connection state changes.

        Returns a function that removes the listener, for easy use in
        async_added_to_hass / async_will_remove_from_hass.
        """
        self._listeners.append(update_callback)

        def _remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    @property
    def previous_notification_value(self) -> Optional[str]:
        """Return the last distinct value seen before the current one."""
        return self._previous_notification_value

    @property
    def previous_notification_data(self) -> Optional[dict]:
        """Return the parsed fields of the previous distinct notification, if any."""
        return self._previous_notification_data

    async def update(self):
        if self._read_uuid is None:
            return
        try:
            data = await self.read_gatt(self._read_uuid)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Unable to read GATT characteristic on update", exc_info=True)
            return

        message, parsed = self._decode_data(data)
        self._update_notification_value(message, parsed)

    async def stop(self):
        """Called on integration unload/reload - make sure we actually let go of the connection."""
        await self.disconnect()
        self._notify_uuid = None
        self._read_uuid = None
        self._notification_callback = None
        self._state_callbacks = []

    @property
    def connected(self):
        return self._client is not None

    @property
    def idle_disconnect_enabled(self) -> bool:
        return self._idle_disconnect_enabled

    @property
    def idle_disconnect_seconds(self) -> Optional[float]:
        return self._idle_disconnect_seconds

    def set_idle_disconnect(self, enabled: bool) -> None:
        """Manual toggle - turn idle-disconnect on/off at runtime."""
        self._idle_disconnect_enabled = enabled
        if enabled:
            self._reset_idle_timer()
        else:
            self._cancel_idle_timer()

    def set_idle_disconnect_seconds(self, seconds: Optional[float]) -> None:
        """Adjust the idle window at runtime."""
        self._idle_disconnect_seconds = seconds
        self._idle_disconnect_enabled = bool(seconds)
        if self._idle_disconnect_enabled and self.connected:
            self._reset_idle_timer()
        else:
            self._cancel_idle_timer()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer_handle is not None:
            self._idle_timer_handle.cancel()
            self._idle_timer_handle = None

    def _reset_idle_timer(self) -> None:
        """(Re)start the idle countdown. Call this after any successful activity."""
        self._cancel_idle_timer()
        if not self._idle_disconnect_enabled or not self._idle_disconnect_seconds:
            return
        loop = asyncio.get_event_loop()
        self._idle_timer_handle = loop.call_later(
            self._idle_disconnect_seconds,
            lambda: asyncio.create_task(self._idle_disconnect()),
        )

    async def _idle_disconnect(self) -> None:
        _LOGGER.debug("Idle timeout reached, disconnecting")
        await self.disconnect()

    async def disconnect(self) -> None:
        """Tear down the BLE connection now, whether idle-timeout or manual/unload triggered.

        Note: self._notify_uuid (the desired subscription target) is
        deliberately left untouched here - get_client() uses it to
        automatically re-arm notifications the next time we reconnect.
        """
        async with self._lock:
            self._cancel_idle_timer()
            if self._client is None:
                return
            _LOGGER.debug("Disconnecting")
            try:
                if self._client_uses_context_manager:
                    await self._client_stack.aclose()
                else:
                    await self._client.disconnect()
            except BleakError:
                _LOGGER.debug("Error while disconnecting", exc_info=True)
            except AttributeError:
                _LOGGER.debug("Client does not support disconnect()", exc_info=True)
            finally:
                self._client = None
                self._client_uses_context_manager = False
                self._notify_active = False
        self._notify_listeners()

    async def get_client(self):
        was_connected = self.connected
        async with self._lock:
            if not self._client:
                _LOGGER.debug("Connecting")
                try:
                    if establish_connection is not None:
                        self._client = await establish_connection(
                            BleakClient,
                            self._ble_device,
                            self._ble_device.address,
                            timeout=30,
                        )
                        self._client_uses_context_manager = False
                    else:
                        self._client = await self._client_stack.enter_async_context(BleakClient(self._ble_device, timeout=30))
                        self._client_uses_context_manager = True
                except asyncio.TimeoutError as exc:
                    _LOGGER.debug("Timeout on connect", exc_info=True)
                    raise GenericBTTimeoutError("Timeout on connect") from exc
                except BleakError as exc:
                    _LOGGER.debug("Error on connect", exc_info=True)
                    raise GenericBTBleakError("Error on connect") from exc
            else:
                _LOGGER.debug("Connection reused")

            # Whenever we're connected, notifications for the last-desired
            # UUID should be active - this covers the initial subscribe AND
            # every reconnect (idle-disconnect, dropped connection, etc.),
            # so a write made after a reconnect still gets its confirming
            # notification captured instead of silently going nowhere.
            await self._ensure_notify_active()

        # Outside the lock - resetting the timer doesn't need to block on it,
        # and we don't want to deadlock if this ever gets called from within
        # the idle-disconnect callback path.
        self._reset_idle_timer()
        if not was_connected:
            self._notify_listeners()

    async def _ensure_notify_active(self) -> None:
        """(Re)arm notifications on the current client for the desired UUID.

        No-op if there's no desired subscription, no client, or it's
        already armed. Must be called while holding self._lock (or before
        any other task could observe self._client in an inconsistent state).
        """
        if self._notify_uuid is None or self._client is None or self._notify_active:
            return
        _LOGGER.debug("(Re)arming notifications for UUID %s", self._notify_uuid)
        uuid = self._to_uuid(self._notify_uuid)
        await self._client.start_notify(uuid, self._handle_notification)
        self._notify_active = True

        # Do an immediate read to seed the current value. This talks to
        # self._client directly rather than going through update()/
        # read_gatt(), since those call get_client() and would deadlock
        # trying to reacquire self._lock, which we're already holding here.
        if self._read_uuid is not None:
            try:
                raw = await self._client.read_gatt_char(self._to_uuid(self._read_uuid))
                message, parsed = self._decode_data(raw)
                self._update_notification_value(message, parsed)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("Unable to read the initial GATT value after (re)subscribe", exc_info=True)

    def set_state_callback(self, callback: Callable[[], None]) -> None:
        if callback not in self._state_callbacks:
            self._state_callbacks.append(callback)

    def _update_notification_value(self, message: Optional[str], parsed: Optional[dict] = None) -> None:
        if message is None or self.last_notification_value == message:
            return
        self._previous_notification_value = self.last_notification_value
        self._previous_notification_data = self.last_notification_data
        self.last_notification_value = message
        self.last_notification_data = parsed
        if self._notification_callback is not None:
            self._notification_callback(message)
        for state_callback in list(self._state_callbacks):
            with suppress(Exception):
                state_callback()

    def _decode_data(self, data) -> tuple[Optional[str], Optional[dict]]:
        """Decode a raw GATT payload.

        Returns (display_value, parsed_fields). The payload is a fixed-size
        binary struct (see parse_settings_packet), not text - it must never
        be run through a text codec, since that produces garbage/misleading
        results (e.g. a stray byte pair decoding to "ON", or non-ASCII bytes
        turning into mojibake).
        """
        if data is None:
            return None, None
        if isinstance(data, (bytes, bytearray)):
            raw_bytes = bytes(data)
            if not raw_bytes:
                return "", None

            if len(raw_bytes) >= SETTINGS_PACKET_LENGTH:
                try:
                    parsed = parse_settings_packet(raw_bytes)
                except (struct.error, ValueError):
                    _LOGGER.debug("Failed to parse settings packet: %s", raw_bytes.hex(), exc_info=True)
                    return raw_bytes.hex(), None
                return raw_bytes.hex(), parsed

            # Unknown/shorter binary payload - surface it as hex rather than
            # guessing a text encoding.
            return raw_bytes.hex(), None
        return str(data), None

    def _handle_notification(self, _sender, data) -> None:
        raw_data = data
        message, parsed = self._decode_data(data)
        _LOGGER.debug("Received notification payload=%r decoded=%r parsed=%r", raw_data, message, parsed)
        self._update_notification_value(message, parsed)

    async def write_gatt(self, target_uuid, data):
        await self.get_client()
        uuid_str = "{" + target_uuid + "}"
        uuid = UUID(uuid_str)
        data_as_bytes = bytearray.fromhex(data)
        await self._client.write_gatt_char(uuid, data_as_bytes, True)
        self._reset_idle_timer()

    async def read_gatt(self, target_uuid):
        await self.get_client()
        uuid_str = "{" + target_uuid + "}"
        uuid = UUID(uuid_str)
        data = await self._client.read_gatt_char(uuid)
        self._reset_idle_timer()
        return data

    def _to_uuid(self, target_uuid):
        uuid_str = "{" + target_uuid + "}"
        return UUID(uuid_str)

    async def subscribe_to_notify(self, target_uuid, callback=None):
        """Set the desired notify subscription and ensure it's active.

        This is now just a "declare what I want" call - the actual arming
        (start_notify) happens inside get_client(), which re-runs it on
        every connect/reconnect. That means once a subscription has been
        requested, it stays in effect for the life of the device: an
        idle-disconnect followed by a write_gatt() will transparently
        reconnect AND re-arm notifications, so the response to that write
        still gets captured.
        """
        if self._notify_uuid is not None and self._notify_uuid != target_uuid:
            await self.unsubscribe_from_notify(self._notify_uuid)

        self._notify_uuid = target_uuid
        self._read_uuid = target_uuid
        self._notification_callback = callback

        _LOGGER.debug("Subscription target set to UUID %s", target_uuid)
        await self.get_client()
        self._reset_idle_timer()

    async def unsubscribe_from_notify(self, target_uuid):
        if self._notify_uuid != target_uuid:
            return

        _LOGGER.debug("Unsubscribing from notifications for UUID %s", target_uuid)
        if self._client is not None:
            with suppress(Exception):
                await self._client.stop_notify(self._to_uuid(target_uuid))

        self._notify_uuid = None
        self._read_uuid = None
        self._notification_callback = None
        self._notify_active = False

    def update_from_advertisement(self, advertisement):
        pass