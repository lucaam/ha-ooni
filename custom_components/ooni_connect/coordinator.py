from datetime import timedelta
import logging
import asyncio
import time
from typing import Any

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Minimum seconds between connection attempts to avoid hammering the device
_MIN_RETRY_INTERVAL = 30

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
            if (self._connection_task is None or self._connection_task.done()) and \
                    (now - self._last_connect_attempt) >= _MIN_RETRY_INTERVAL:
                self._last_connect_attempt = now
                self._connection_task = self.hass.async_create_task(self._connect_in_background())

        return self._last_data

    async def _connect_in_background(self) -> None:
        """Attempts to establish the BLE connection in the background without blocking HA."""
        try:
            from ooni_connect_bluetooth.client import Client
        except ImportError as import_err:
            _LOGGER.error("Cannot import ooni_connect_bluetooth: %s", import_err)
            return

        async with self._lock:
            _LOGGER.debug("Looking for BLE device with address %s...", self.address)
            ble_device = async_ble_device_from_address(self.hass, self.address, connectable=True)
            if not ble_device:
                _LOGGER.debug("Ooni device not reachable at address: %s", self.address)
                return

            _LOGGER.info("BLE device found: %s", ble_device.name or ble_device.address)

            try:
                _LOGGER.info("Establishing background connection to Ooni...")
                # establish_connection (bleak_retry_connector) manages its own retries and timeouts.
                # Do NOT wrap with asyncio.timeout here — it would cut off the retry cycle prematurely.
                self.client = await Client.connect(
                    device=ble_device,
                    notify_callback=self._handle_bluetooth_update,
                    disconnected_callback=self._on_disconnected
                )
                _LOGGER.info("Ooni successfully connected in the background")
            except Exception as err:
                _LOGGER.error("Background connection failed: %s (%s)", err, type(err).__name__, exc_info=True)
                self.client = None
