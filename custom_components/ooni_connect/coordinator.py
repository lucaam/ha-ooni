from datetime import timedelta
import logging
import asyncio
import time
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import establish_connection, BleakOutOfConnectionSlotsError
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Minimum seconds to wait after a normal failed connection attempt before retrying
_MIN_RETRY_INTERVAL = 60
# Longer backoff when the proxy runs out of connection slots;
# slots are held for ~30-60s after a dropped attempt, so wait longer.
_OUT_OF_SLOTS_RETRY_INTERVAL = 300

class OoniConnectCoordinator(DataUpdateCoordinator[Any]):
    """Manages the Ooni DT Hub BLE connection."""

    def __init__(self, hass: HomeAssistant, address: str, name: str):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=60),
        )
        self.address = address
        self.device_name = name
        self.client = None
        self._last_data = None
        self._lock = asyncio.Lock()
        self._connection_task = None
        self._last_connect_attempt: float = 0
        self._connecting: bool = False

    @property
    def is_connected(self) -> bool:
        """Return True only when the BLE client is active and connected."""
        return self.client is not None and self.client.is_connected

    def _handle_bluetooth_update(self, data: Any) -> None:
        """Callback for incoming data packets."""
        _LOGGER.debug("Ooni data received: %s", data)

        # DEBUG: log all received data
        _LOGGER.info(">>> BLUETOOTH UPDATE RECEIVED <<<")
        _LOGGER.info("Data is None: %s", data is None)
        _LOGGER.info("Data type: %s", type(data).__name__)

        if data is not None:
            _LOGGER.info("Data repr: %s", repr(data))
            if hasattr(data, '__dict__'):
                _LOGGER.info("Data.__dict__: %s", data.__dict__)
            _LOGGER.info("Dir(data): %s", [x for x in dir(data) if not x.startswith('_')])

        _LOGGER.info(">>> END BLUETOOTH UPDATE <<<")

        self._last_data = data
        self.async_set_updated_data(data)

    def _on_disconnected(self) -> None:
        """Callback for connection loss."""
        _LOGGER.warning("Ooni connection lost")
        self.client = None

    async def _async_update_data(self) -> Any:
        """Called periodically by HA to refresh data."""
        if self.client is None or not self.client.is_connected:
            now = time.monotonic()
            if not self._connecting and \
                    (now - self._last_connect_attempt) >= _MIN_RETRY_INTERVAL:
                # Set both the flag AND the timestamp BEFORE creating the task:
                # - flag prevents a second task from being spawned before this one starts
                # - timestamp prevents an immediate re-spawn if the task fails very quickly
                #   (e.g. before establish_connection is even reached)
                self._connecting = True
                self._last_connect_attempt = now
                self._connection_task = self.hass.async_create_task(self._connect_in_background())

        return self._last_data

    async def _connect_in_background(self) -> None:
        """Attempts to establish the BLE connection in the background without blocking HA."""
        try:
            from ooni_connect_bluetooth.client import Client
            from ooni_connect_bluetooth.packets import PacketNotify
            from ooni_connect_bluetooth.services import NotifyCharacteristic
            from ooni_connect_bluetooth.const import MainService
            from ooni_connect_bluetooth.exceptions import DecodeError
        except ImportError as import_err:
            _LOGGER.error("Cannot import ooni_connect_bluetooth: %s", import_err)
            self._connecting = False
            return

        try:
            async with self._lock:
                _LOGGER.debug("Looking for BLE device with address %s...", self.address)
                ble_device = async_ble_device_from_address(self.hass, self.address, connectable=True)
                if not ble_device:
                    _LOGGER.debug("Ooni device not reachable at address: %s", self.address)
                    return

                _LOGGER.info("BLE device found: %s", ble_device.name or ble_device.address)

                try:
                    _LOGGER.info("Establishing background connection to Ooni...")
                    # Use establish_connection directly so we can limit max_attempts.
                    # Keeping it low (3) avoids exhausting all proxy slots in a single run.

                    def _disconnected_callback(bleak_client: BleakClient) -> None:
                        _LOGGER.info("Device disconnected %s", bleak_client.address)
                        self._on_disconnected()

                    bleak_client = await establish_connection(
                        BleakClient,
                        device=ble_device,
                        name="Ooni Connect Connection",
                        disconnected_callback=_disconnected_callback,
                        max_attempts=3,
                    )

                    client = Client(bleak_client, None)

                    def _notify_data(char: BleakGATTCharacteristic, data: bytearray) -> None:
                        try:
                            packet_data = NotifyCharacteristic.decode(data)
                            packet = PacketNotify.decode(packet_data)
                        except DecodeError as exc:
                            _LOGGER.error("Failed to decode: %s with error %s", data, exc)
                            return
                        self._handle_bluetooth_update(packet)

                    await bleak_client.start_notify(MainService.notify.uuid, _notify_data)

                    if bleak_client.is_connected:
                        self.client = client
                        _LOGGER.info("Ooni successfully connected in the background")
                    else:
                        _LOGGER.error("Connection returned but device is not connected")
                        self.client = None
                except BleakOutOfConnectionSlotsError as err:
                    _LOGGER.warning(
                        "Proxy out of connection slots — waiting %ds before next attempt: %s",
                        _OUT_OF_SLOTS_RETRY_INTERVAL, err,
                    )
                    self.client = None
                    # Override the cooldown to a longer value
                    self._last_connect_attempt = time.monotonic() + _OUT_OF_SLOTS_RETRY_INTERVAL - _MIN_RETRY_INTERVAL
                except Exception as err:
                    _LOGGER.error("Background connection failed: %s (%s)", err, type(err).__name__, exc_info=True)
                    self.client = None
        finally:
            self._connecting = False
            if self.client is None or not self.client.is_connected:
                # Normalize a disconnected-but-not-None client to None
                self.client = None
                # Reset the cooldown timestamp from the actual failure time, not from
                # when the attempt started (set in _async_update_data). This ensures
                # the full _MIN_RETRY_INTERVAL is respected after a long-running failure.
                self._last_connect_attempt = time.monotonic()

    async def async_disconnect(self) -> None:
        """Disconnect from the device and cancel any pending connection task."""
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            self._connection_task = None
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
