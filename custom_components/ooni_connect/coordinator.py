from datetime import timedelta
import logging
import asyncio
from typing import Any

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

class OoniConnectCoordinator(DataUpdateCoordinator[Any]):
    """Verwaltung der Ooni DT Hub Verbindung mit schnellem Start."""

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

    def _handle_bluetooth_update(self, data: Any) -> None:
        """Callback für Datenpakete."""
        _LOGGER.debug("Ooni Daten empfangen: %s", data)

        # DEBUG: Logging für alle empfangenen Daten
        _LOGGER.info(">>> BLUETOOTH UPDATE EMPFANGEN <<<")
        _LOGGER.info("Data ist None: %s", data is None)
        _LOGGER.info("Data type: %s", type(data).__name__)

        if data is not None:
            _LOGGER.info("Data repr: %s", repr(data))
            if hasattr(data, '__dict__'):
                _LOGGER.info("Data.__dict__: %s", data.__dict__)
            _LOGGER.info("Dir(data): %s", [x for x in dir(data) if not x.startswith('_')])

        _LOGGER.info(">>> ENDE BLUETOOTH UPDATE <<<")

        self._last_data = data
        self.async_set_updated_data(data)

    def _on_disconnected(self) -> None:
        """Callback bei Verbindungsverlust."""
        _LOGGER.warning("Ooni Verbindung getrennt")
        self.client = None

    async def _async_update_data(self) -> Any:
        """Wird von HA regelmäßig aufgerufen."""
        # Wenn kein Task läuft und wir nicht verbunden sind, starte Verbindungsversuch im Hintergrund
        if self.client is None or not self.client.is_connected:
            if self._connection_task is None or self._connection_task.done():
                self._connection_task = self.hass.async_create_task(self._connect_in_background())

        return self._last_data

    async def _connect_in_background(self) -> None:
        """Versucht die Verbindung im Hintergrund aufzubauen, ohne HA zu blockieren."""
        try:
            from ooni_connect_bluetooth.client import Client
        except ImportError as import_err:
            _LOGGER.error("Kann ooni_connect_bluetooth nicht importieren: %s", import_err)
            return

        async with self._lock:
            _LOGGER.debug("Versuche BLE Gerät mit Adresse %s zu finden...", self.address)
            ble_device = async_ble_device_from_address(self.hass, self.address)
            if not ble_device:
                _LOGGER.debug("Ooni Gerät nicht erreichbar mit Adresse: %s", self.address)
                return

            _LOGGER.info("BLE Gerät gefunden: %s", ble_device.name or ble_device.address)

            try:
                _LOGGER.info("Hintergrund-Verbindung zu Ooni wird aufgebaut...")
                # Timeout für den Connect, damit der Task nicht ewig hängt
                async with asyncio.timeout(20):
                    self.client = await Client.connect(
                        device=ble_device,
                        notify_callback=self._handle_bluetooth_update,
                        disconnected_callback=self._on_disconnected
                    )
                _LOGGER.info("✓ Ooni im Hintergrund erfolgreich verbunden")
                _LOGGER.info("Client ist: %s", self.client)
                _LOGGER.info("Notify callback registriert: %s", self._handle_bluetooth_update)
            except asyncio.TimeoutError:
                _LOGGER.error("Timeout beim Verbinden mit Ooni (20 Sekunden)")
                self.client = None
            except Exception as err:
                _LOGGER.error("Hintergrund-Verbindung fehlgeschlagen. Fehler: %s (%s)", err, type(err).__name__, exc_info=True)
                self.client = None
