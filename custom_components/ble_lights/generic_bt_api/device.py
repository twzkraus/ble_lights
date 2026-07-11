"""generic bt device"""

from uuid import UUID
from typing import Callable, Optional
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import struct
from contextlib import AsyncExitStack, suppress
from ..const import DEFAULT_IDLE_DISCONNECT_SECONDS, DIRECTION_MAPPING, NOTIFICATION_REASSEMBLY_TIMEOUT_SECONDS, NUM_COLOR_SLOTS, POLL_TIMER_EVENT_BUFFER_SECONDS, PROGRAM_MAPPING, REQUEST_SETTINGS_COMMAND_HEX, SETTINGS_PACKET_LENGTH, SYNC_MODE_MAPPING

from bleak import BleakClient
from bleak.exc import BleakError

try:
    from bleak_retry_connector import establish_connection
except ImportError:  # pragma: no cover - optional dependency in tests
    establish_connection = None

_LOGGER = logging.getLogger(__name__)

_TIMER_STRUCT = struct.Struct("<7B")  # timerOnOff, startSunset, startHour, startMinute, endSunrise, endHour, endMinute

MAX_EXPECTED_VERSION = 50

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

# --- Low-level write protocol -------------------------------------------
# Mirrors the read-side layout above: a prefix byte (device's internal
# command id) + an ASCII command name + argument bytes. Centralized here
# so every consumer - light.py, apply_scene_packet, future entities -
# goes through the same encoding instead of each building hex itself.

CMD_LIGHTS_ON_PREFIX = 0x08
CMD_LIGHTS_OFF_PREFIX = 0x09
CMD_PROGRAM_PREFIX = 0x08
CMD_SET_COLORS_PREFIX = 0x12
CMD_BRIGHTNESS_PREFIX = 0x0B
CMD_SPEED_PREFIX = 0x06
CMD_DIRECTION_PREFIX = 0x0A

ASCII_LIGHTS_ON = "lightsOn"
ASCII_LIGHTS_OFF = "lightsOff"
ASCII_BRIGHTNESS = "brightness"
ASCII_SPEED = "speed"
ASCII_DIRECTION = "direction"


def _ascii_command(prefix: int, ascii_payload: str) -> str:
    """Build a hex payload: 1 prefix byte + ascii-encoded payload."""
    return f"{prefix:02X}" + ascii_payload.encode("ascii").hex().upper()


def _encode_colors_hsv(hsv_colors: list[tuple[int, int, int]]) -> str:
    """Encode 1-6 native (H, S, V) byte triples into the device's hex payload.

    Unlike _encode_colors (RGB, UI-facing) this takes bytes already in the
    device's own HSV space - e.g. straight out of parse_settings_packet -
    with no color-space conversion.
    """
    if not 1 <= len(hsv_colors) <= NUM_COLOR_SLOTS:
        raise ValueError(f"Provide 1-{NUM_COLOR_SLOTS} colors, got {len(hsv_colors)}")
    payload = f"{CMD_SET_COLORS_PREFIX:02X}"
    for h, s, v in hsv_colors[:NUM_COLOR_SLOTS]:
        payload += f"{h:02X}{s:02X}{v:02X}"

    # Fill ALL remaining empty slots with "000000"
    if len(hsv_colors) < NUM_COLOR_SLOTS:
        remaining_slots = NUM_COLOR_SLOTS - len(hsv_colors)
        payload += "000000" * remaining_slots

    return payload


def _rgb_to_hsv_bytes(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Convert an 0-255 RGB tuple into single-byte H, S, V values."""
    import colorsys

    r, g, b = (c / 255 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return (round(h * 255), round(s * 255), round(v * 255))


def _encode_colors(colors: list[tuple[int, int, int]]) -> str:
    """Encode 1-6 RGB colors (UI-facing) into the device's hex payload."""
    return _encode_colors_hsv([_rgb_to_hsv_bytes(rgb) for rgb in colors])


def _encode_brightness(value: int) -> str:
    return _ascii_command(CMD_BRIGHTNESS_PREFIX, ASCII_BRIGHTNESS) + f"{value:02X}"


def _encode_speed(value: int) -> str:
    return _ascii_command(CMD_SPEED_PREFIX, ASCII_SPEED) + f"{value:02X}"


def _encode_direction(code: int) -> str:
    return _ascii_command(CMD_DIRECTION_PREFIX, ASCII_DIRECTION) + f"{code:02X}"


def _encode_effect(code: str) -> str:
    return _ascii_command(CMD_PROGRAM_PREFIX, f"program{code}")

@dataclass(frozen=True)
class DeviceSettings:
    """Structured result of parse_settings_packet."""
    program_code: str
    program_name: str
    speed: int
    colors: list[dict]
    is_on: bool
    timer1: dict
    timer2: dict
    brightness: int
    version: int
    sync_code: int
    sync_name: str
    direction_code: int
    direction_name: str
    raw_hex: str
    last_updated: str

def parse_settings_packet(raw_bytes: bytes) -> DeviceSettings:
    """Parse the 40-byte requestSettings response into structured fields."""
    if len(raw_bytes) < SETTINGS_PACKET_LENGTH:
        raise ValueError(
            f"Expected at least {SETTINGS_PACKET_LENGTH} bytes, got {len(raw_bytes)}: {raw_bytes.hex()}"
        )

    # Slice exactly the first 40 bytes to ensure consistency
    packet = raw_bytes[:SETTINGS_PACKET_LENGTH]

    # --- SWAPPED PACKET CORRECTION ---
    # Check BOTH possible orientations (as-received, and first & last 20 swapped)
    # enum-backed fields. Trust whichever is uniquely self-consistent.
    def _is_plausible_orientation(candidate: bytes) -> bool:
        program_byte = candidate[0:1]
        on_off = candidate[20]
        version = candidate[36]
        sync = candidate[37]
        direction = candidate[38]

        return (
            program_byte in PROGRAM_MAPPING
            and on_off in (0, 1)
            and version <= MAX_EXPECTED_VERSION
            and sync in SYNC_MODE_MAPPING
            and direction in DIRECTION_MAPPING
        )

    swapped = packet[20:] + packet[:20]

    as_received_ok = _is_plausible_orientation(packet)
    as_swapped_ok = _is_plausible_orientation(swapped)

    if as_received_ok and not as_swapped_ok:
        pass
    elif as_swapped_ok and not as_received_ok:
        packet = swapped
    elif not as_received_ok and not as_swapped_ok:
        raise ValueError(
            f"Malformatted packet detected: neither orientation produces a valid "
            f"program byte, on/off flag, version, sync, and direction combination: "
            f"{raw_bytes.hex()}"
        )
    else:
        # Both orientations pass every check individually — genuinely ambiguous.
        # Rare in practice.
        _LOGGER.warning(
            "Ambiguous packet: both orientations look valid, keeping as received: %s",
            raw_bytes.hex(),
        )

    # ---------------------------------

    # 1. Program and speed
    program_byte = packet[0:1]
    program_code = program_byte.decode('ascii', errors='replace')
    program_name = PROGRAM_MAPPING.get(program_byte, "Unknown")
    speed = packet[1]

    # 2. Colors (6 slots of HSV, 3 bytes each)
    colors = []
    offset = 2
    for _ in range(6):
        hue, saturation, value = packet[offset:offset + 3]
        colors.append({"hue": hue, "saturation": saturation, "value": value})
        offset += 3

    # 3. On/Off Switch
    on_off_switch = packet[offset]
    offset += 1

    # 4. Timers
    timer1 = _parse_timer(packet[offset:offset + 7])
    offset += 7
    timer2 = _parse_timer(packet[offset:offset + 7])
    offset += 7

    # 5. Miscellaneous Settings
    brightness = packet[offset]
    offset += 1
    version = packet[offset]
    offset += 1

    sync_code = packet[offset]
    sync_name = SYNC_MODE_MAPPING.get(sync_code, "Unknown")
    offset += 1

    direction_code = packet[offset]
    direction_name = DIRECTION_MAPPING.get(direction_code, "Unknown")
    offset += 1

    return DeviceSettings(
        program_code=program_code,
        program_name=program_name,
        speed=speed,
        colors=colors,
        is_on=bool(on_off_switch),
        timer1=timer1,
        timer2=timer2,
        brightness=brightness,
        version=version,
        sync_code=sync_code,
        sync_name=sync_name,
        direction_code=direction_code,
        direction_name=direction_name,
        raw_hex=packet.hex(),
        last_updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


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
        self.last_notification_data: Optional[DeviceSettings] = None

        # Fragmented-notification reassembly. Bytes accumulate here across
        # multiple _handle_notification calls until we have a full packet
        # (or the reassembly timer fires and we give up on this attempt).
        self._notification_buffer: bytearray = bytearray()
        self._reassembly_timer_handle: Optional[asyncio.TimerHandle] = None

        # Futures for callers awaiting the next complete response via
        # request_and_wait()/request_settings().
        self._pending_response_futures: list[asyncio.Future] = []

        # Periodic polling bookkeeping (see "--- Polling ---" section below).
        self._poll_timer_handle: Optional[asyncio.TimerHandle] = None
        self._poll_write_uuid: Optional[str] = None
        self._polling_enabled: bool = False
        # loop.time()-based timestamp of the next scheduled poll, exposed
        # mainly for diagnostics/tests.
        self.next_poll_time: Optional[float] = None

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

    async def async_refresh_settings(
        self,
        write_uuid: str,
        notify_uuid: Optional[str] = None,
        timeout: float = NOTIFICATION_REASSEMBLY_TIMEOUT_SECONDS,
    ) -> DeviceSettings:
        """Connect, request settings, and guarantee the result is stored + published.

        This is the one entry point periodic polling and the sync_state
        entity service should both call. Raises on timeout.
        """
        parsed = await self.request_and_wait(
            write_uuid, REQUEST_SETTINGS_COMMAND_HEX, notify_uuid=notify_uuid, timeout=timeout
        )
        if parsed is None:
            raise GenericBTTimeoutError(
                f"No complete response to requestSettings within {timeout}s"
            )
        return parsed

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
        self.stop_polling()
        await self.disconnect()
        self._notify_uuid = None
        self._read_uuid = None
        self._notification_callback = None
        self._state_callbacks = []
        self._cancel_reassembly_timer()
        self._notification_buffer = bytearray()
        self._fail_pending_response_futures(RuntimeError("Device stopped"))

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

            try:
                await self._ensure_notify_active()
            except BleakError as exc:
                # The client object claimed to be connected but a GATT op just
                # failed - the underlying connection is actually dead. Drop the
                # stale client so the *next* get_client() call does a real
                # reconnect instead of trusting this handle forever.
                _LOGGER.debug("Stale connection detected while (re)arming notifications, discarding client", exc_info=True)
                self._client = None
                self._client_uses_context_manager = False
                self._notify_active = False
                raise GenericBTBleakError("Stale connection while arming notifications") from exc

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

        def _remove() -> None:
            if callback in self._state_callbacks:
                self._state_callbacks.remove(callback)

        return _remove

    def _update_notification_value(self, message: Optional[str], parsed: Optional[DeviceSettings] = None) -> None:
        """Record the latest decoded reading and always publish it.
        """
        if message is None:
            return
        self.last_notification_value = message
        if parsed is not None:
            self.last_notification_data = parsed
        if self._notification_callback is not None:
            self._notification_callback(message)
        for state_callback in list(self._state_callbacks):
            with suppress(Exception):
                state_callback()
        self._schedule_next_poll()

    def _decode_data(self, data) -> tuple[Optional[str], Optional[DeviceSettings]]:
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
        """Accumulate a notification fragment; surface the value only once a full packet is assembled.

        BLE notifications can arrive split across multiple MTU-limited
        fragments (observed: 5, 15, then 20 bytes for one 40-byte packet).
        Nothing is written to last_notification_value/last_notification_data
        - and therefore no sensor updates - until we've accumulated a full
        SETTINGS_PACKET_LENGTH worth of bytes. Partial state is never
        exposed.
        """
        fragment = bytes(data)
        _LOGGER.debug(
            "Received notification fragment len=%d bytes=%s (buffered before=%d)",
            len(fragment), fragment.hex(), len(self._notification_buffer),
        )

        if not self._notification_buffer:
            self._start_reassembly_timer()

        self._notification_buffer.extend(fragment)

        if len(self._notification_buffer) >= SETTINGS_PACKET_LENGTH:
            self._complete_reassembly()

    def _start_reassembly_timer(self) -> None:
        self._cancel_reassembly_timer()
        loop = asyncio.get_event_loop()
        self._reassembly_timer_handle = loop.call_later(
            NOTIFICATION_REASSEMBLY_TIMEOUT_SECONDS, self._on_reassembly_timeout
        )

    def _cancel_reassembly_timer(self) -> None:
        if self._reassembly_timer_handle is not None:
            self._reassembly_timer_handle.cancel()
            self._reassembly_timer_handle = None

    def _on_reassembly_timeout(self) -> None:
        """Give up on an incomplete fragment sequence."""
        self._reassembly_timer_handle = None
        if self._notification_buffer:
            _LOGGER.debug(
                "Discarding %d incomplete notification bytes after %.0fs reassembly timeout: %s",
                len(self._notification_buffer),
                NOTIFICATION_REASSEMBLY_TIMEOUT_SECONDS,
                bytes(self._notification_buffer).hex(),
            )
        self._notification_buffer = bytearray()
        self._fail_pending_response_futures(
            TimeoutError(f"No complete {SETTINGS_PACKET_LENGTH}-byte response within "
                         f"{NOTIFICATION_REASSEMBLY_TIMEOUT_SECONDS}s")
        )

    def _complete_reassembly(self) -> None:
        """We have >= a full packet's worth of bytes - decode it and surface the result."""
        self._cancel_reassembly_timer()

        complete_bytes = bytes(self._notification_buffer[:SETTINGS_PACKET_LENGTH])
        leftover = self._notification_buffer[SETTINGS_PACKET_LENGTH:]
        self._notification_buffer = bytearray(leftover)

        message, parsed = self._decode_data(complete_bytes)
        _LOGGER.debug("Reassembled complete notification payload=%r parsed=%r", message, parsed)
        self._update_notification_value(message, parsed)

        if parsed is not None:
            self._resolve_pending_response_futures(parsed)
        else:
            _LOGGER.debug(
                "Discarding malformed reassembled packet, leaving pending futures outstanding: %s",
                complete_bytes.hex(),
            )

        # Rare: the device sent us the start of a *second* packet in the
        # same batch of fragments. Give it its own reassembly window rather
        # than silently dropping it.
        if self._notification_buffer:
            self._start_reassembly_timer()

    def _resolve_pending_response_futures(self, parsed: Optional[DeviceSettings]) -> None:
        futures, self._pending_response_futures = self._pending_response_futures, []
        for future in futures:
            if not future.done():
                future.set_result(parsed)

    def _fail_pending_response_futures(self, exc: Exception) -> None:
        futures, self._pending_response_futures = self._pending_response_futures, []
        for future in futures:
            if not future.done():
                future.set_exception(exc)

    async def write_gatt(self, target_uuid, data):
        await self.get_client()
        uuid = self._to_uuid(target_uuid)
        data_as_bytes = bytearray.fromhex(data)
        try:
            await self._client.write_gatt_char(uuid, data_as_bytes, True)
        except BleakError as exc:
            _LOGGER.debug("Stale connection detected during write, discarding client", exc_info=True)
            async with self._lock:
                self._client = None
                self._client_uses_context_manager = False
                self._notify_active = False
            raise GenericBTBleakError("Error writing GATT characteristic") from exc
        self._reset_idle_timer()
        self._schedule_next_poll()

    async def read_gatt(self, target_uuid):
        await self.get_client()
        uuid = self._to_uuid(target_uuid)
        try:
            data = await self._client.read_gatt_char(uuid)
        except BleakError as exc:
            _LOGGER.debug("Stale connection detected during read, discarding client", exc_info=True)
            async with self._lock:
                self._client = None
                self._client_uses_context_manager = False
                self._notify_active = False
            raise GenericBTBleakError("Error reading GATT characteristic") from exc
        self._reset_idle_timer()
        return data

    def _to_uuid(self, target_uuid):
        uuid_str = "{" + target_uuid + "}"
        return UUID(uuid_str)

    # --- High-level device operations ------------------------------------

    async def turn_on(self, write_uuid: str) -> None:
        await self.write_gatt(write_uuid, _ascii_command(CMD_LIGHTS_ON_PREFIX, ASCII_LIGHTS_ON))

    async def turn_off(self, write_uuid: str) -> None:
        await self.write_gatt(write_uuid, _ascii_command(CMD_LIGHTS_OFF_PREFIX, ASCII_LIGHTS_OFF))

    async def set_colors(self, write_uuid: str, colors: list[tuple[int, int, int]]) -> None:
        """Set 1-6 colors given as 0-255 RGB tuples."""
        await self.write_gatt(write_uuid, _encode_colors(colors))

    async def set_colors_hsv(self, write_uuid: str, hsv_colors: list[tuple[int, int, int]]) -> None:
        """Set 1-6 colors given as native device (H, S, V) byte triples."""
        await self.write_gatt(write_uuid, _encode_colors_hsv(hsv_colors))

    async def set_effect(self, write_uuid: str, program_code: str) -> None:
        """Set the effect/program using its single-character code (see PROGRAM_MAPPING)."""
        await self.write_gatt(write_uuid, _encode_effect(program_code))

    async def set_speed(self, write_uuid: str, value: int) -> None:
        await self.write_gatt(write_uuid, _encode_speed(value))

    async def set_brightness(self, write_uuid: str, value: int) -> None:
        await self.write_gatt(write_uuid, _encode_brightness(value))

    async def set_direction(self, write_uuid: str, code: int) -> None:
        await self.write_gatt(write_uuid, _encode_direction(code))

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

    async def request_and_wait(
        self,
        write_uuid: str,
        command_hex: str,
        *,
        notify_uuid: Optional[str] = None,
        timeout: float = NOTIFICATION_REASSEMBLY_TIMEOUT_SECONDS,
    ) -> Optional[DeviceSettings]:
        """Subscribe (if needed), send a write command, and wait for the next full response.

        Ensures notifications are subscribed on `notify_uuid` (or the
        already-active subscription if `notify_uuid` is omitted), sends
        `command_hex` to `write_uuid`, then waits for a complete
        (SETTINGS_PACKET_LENGTH-byte) notification to be reassembled.

        Returns the parsed settings class, or None if no complete response
        arrived within `timeout` seconds.
        """
        if notify_uuid is not None:
            if self._notify_uuid != notify_uuid:
                await self.subscribe_to_notify(notify_uuid, callback=self._notification_callback)
        elif self._notify_uuid is None:
            raise ValueError("No notify UUID is subscribed and none was provided")
        else:
            # Make sure we're connected and the existing subscription is armed
            # (e.g. after an idle-disconnect) before we send the command.
            await self.get_client()

        response_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_response_futures.append(response_future)
        try:
            await self.write_gatt(write_uuid, command_hex)
            return await asyncio.wait_for(response_future, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            _LOGGER.debug("Timed out waiting for a complete response to command %s", command_hex)
            return None
        finally:
            with suppress(ValueError):
                self._pending_response_futures.remove(response_future)

    async def request_settings(
        self,
        write_uuid: str,
        notify_uuid: Optional[str] = None,
        timeout: float = NOTIFICATION_REASSEMBLY_TIMEOUT_SECONDS,
    ) -> Optional[DeviceSettings]:
        """Convenience wrapper: send the requestSettings command and wait for the parsed reply."""
        return await self.request_and_wait(
            write_uuid, REQUEST_SETTINGS_COMMAND_HEX, notify_uuid=notify_uuid, timeout=timeout
        )

    # --- Polling -----------------------------------------------------------
    # Besides reacting to BLE notifications, we periodically ask the device
    # for its current settings so Home Assistant's view of state doesn't
    # silently drift (e.g. someone used the iOS app, or one of the
    # device's own timers fired).

    def start_polling(self, write_uuid: str) -> None:
        """Begin periodic polling of device state via requestSettings.

        Call once from async_setup_entry, after subscribe_to_notify
        has set up the notification path.
        Safe to call again to change the write UUID; it will just reschedule.
        """
        self._poll_write_uuid = write_uuid
        self._polling_enabled = True
        asyncio.create_task(self._poll())

    def stop_polling(self) -> None:
        """Stop periodic polling, e.g. on integration unload."""
        self._polling_enabled = False
        self._cancel_poll_timer()
        self.next_poll_time = None

    def _cancel_poll_timer(self) -> None:
        if self._poll_timer_handle is not None:
            self._poll_timer_handle.cancel()
            self._poll_timer_handle = None

    def _schedule_next_poll(self, override_delay: float | None = None) -> None:
        """(Re)arm the poll timer.

        Default: fires 30 seconds after the next top of the hour.
        But if the last settings show a timer whose next on/off transition
        is sooner than that, wake up shortly after that instead.

        An optional override_delay can be passed to manually force a poll sooner.

        No-op if polling hasn't been started via start_polling().
        """
        if not self._polling_enabled or self._poll_write_uuid is None:
            return
        self._cancel_poll_timer()

        # 1. Determine the default delay (30 seconds past the next hour)
        if override_delay is not None:
            delay = override_delay
        else:
            now = datetime.now()
            next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            next_poll_target = next_hour + timedelta(seconds=30)
            delay = (next_poll_target - now).total_seconds()

        # 2. Check if a device timer event happens even sooner
        next_timer_event = self._seconds_until_next_timer_event()
        if next_timer_event is not None and next_timer_event < delay:
            delay = next_timer_event

        # 3. Schedule the poll
        loop = asyncio.get_event_loop()
        self.next_poll_time = loop.time() + delay
        self._poll_timer_handle = loop.call_later(
            delay, lambda: asyncio.create_task(self._poll())
        )
        _LOGGER.debug("Next poll scheduled in %.0fs", delay)

    async def _poll(self) -> None:
        """Timer-fired poll: refresh state from the device and notify HA."""
        self._poll_timer_handle = None
        if not self._polling_enabled or self._poll_write_uuid is None:
            return
        _LOGGER.debug("Polling device for current settings")
        success = False
        for attempt in range(2):  # one quick retry before the longer backoff
            try:
                await self.async_refresh_settings(self._poll_write_uuid)
                success = True
                break
            except GenericBTTimeoutError:
                if attempt == 0:
                    _LOGGER.debug("Poll got no usable response, retrying now")
                    continue
                _LOGGER.debug("Poll timed out again on retry", exc_info=True)
            except GenericBTBleakError:
                _LOGGER.debug("Poll failed to connect/communicate with device", exc_info=True)
                break  # connectivity issue - retrying immediately won't help, go to retry with override_delay
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("Unexpected error while polling", exc_info=True)
                break

        if success:
            self._schedule_next_poll()
        else:
            _LOGGER.debug("Poll failed; scheduling retry in 5 minutes")
            self._schedule_next_poll(override_delay=300.0)

    def _seconds_until_next_timer_event(self) -> Optional[float]:
        """Seconds until nearest scheduled on/off transition, or None.

        Only clock-time transitions (start_hour/start_minute etc.) can be
        predicted this way; sunrise/sunset-relative timers depend on sun
        data we don't have here, so those are left to the regular
        cadence.
        TODO: pull in helper to get sunrise/sunset time? unclear what lights use, so add addtl padding
        """
        data = self.last_notification_data
        if not data:
            return None

        now = datetime.now()
        candidates: list[float] = []

        for timer in (data.timer1, data.timer2):
            if not timer or not timer.get("timer_on_off"):
                continue
            for sun_flag_key, hour_key, minute_key in (
                ("start_sunset", "start_hour", "start_minute"),
                ("end_sunrise", "end_hour", "end_minute"),
            ):
                if timer.get(sun_flag_key):
                    continue  # sunrise/sunset-relative - can't predict
                hour, minute = timer.get(hour_key), timer.get(minute_key)
                if hour is None or minute is None:
                    continue
                try:
                    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                except ValueError:
                    continue  # garbage hour/minute value from the device
                if candidate <= now:
                    candidate += timedelta(days=1)
                candidates.append((candidate - now).total_seconds() + POLL_TIMER_EVENT_BUFFER_SECONDS)

        return min(candidates) if candidates else None

    def update_from_advertisement(self, advertisement):
        pass