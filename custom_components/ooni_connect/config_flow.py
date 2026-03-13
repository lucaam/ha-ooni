import asyncio
import logging
from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_ble_device_from_address,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class OoniConnectConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ooni Connect Bluetooth."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, str] = {}
        self._address: str = ""
        self._name: str = ""
        self._rssi: int | None = None
        self._connection_failed: bool = False

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        device_name = discovery_info.name or discovery_info.address
        if not device_name.upper().startswith("OONI"):
            return self.async_abort(reason="not_ooni_device")

        self._discovery_info = discovery_info
        self._address = discovery_info.address
        self._name = device_name
        self._rssi = discovery_info.rssi
        self.context["title_placeholders"] = {"name": device_name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        if user_input is not None:
            return await self.async_step_connection_check()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user (manual search)."""
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            self._name = self._discovered_devices[self._address]
            # Find RSSI from the discovery info if available
            for info in async_discovered_service_info(self.hass):
                if info.address == self._address:
                    self._rssi = info.rssi
                    break
            return await self.async_step_connection_check()

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass):
            address = discovery_info.address
            if address in current_addresses:
                continue
            name = discovery_info.name or address
            if name.upper().startswith("OONI"):
                self._discovered_devices[address] = name

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices)}
            ),
        )

    async def async_step_connection_check(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Attempt a BLE connection and warn the user if it fails."""
        if user_input is not None:
            # User acknowledged the warning (or form was submitted) — create the entry
            return self.async_create_entry(
                title=self._name,
                data={CONF_ADDRESS: self._address, CONF_NAME: self._name},
            )

        # If the device is already connected via an existing coordinator, skip the
        # test to avoid kicking out the live connection.
        if self._is_already_connected():
            return self.async_create_entry(
                title=self._name,
                data={CONF_ADDRESS: self._address, CONF_NAME: self._name},
            )

        # Try to connect once to check reachability
        connection_ok = await self._try_connect()

        if connection_ok:
            return self.async_create_entry(
                title=self._name,
                data={CONF_ADDRESS: self._address, CONF_NAME: self._name},
            )

        # Connection failed — show warning.
        # IMPORTANT: use step_id="connection_check" (same as this method) so that
        # when the user submits the form, HA calls async_step_connection_check again
        # with user_input != None and we create the entry without a missing-handler error.
        _LOGGER.warning(
            "Config flow connection check failed for %s (%s)", self._name, self._address
        )
        return self.async_show_form(
            step_id="connection_check",
            description_placeholders={
                "name": self._name,
                "rssi": str(self._rssi) if self._rssi is not None else "unknown",
            },
            errors={"base": "cannot_connect"},
        )

    def _is_already_connected(self) -> bool:
        """Return True if any existing coordinator is already connected to this address."""
        for coordinator in self.hass.data.get(DOMAIN, {}).values():
            if getattr(coordinator, "address", None) == self._address and \
                    getattr(coordinator, "is_connected", False):
                return True
        return False

    async def _try_connect(self) -> bool:
        """Try a single BLE connection attempt with a short timeout. Returns True on success."""
        try:
            from bleak import BleakClient
            from bleak_retry_connector import establish_connection
        except ImportError:
            _LOGGER.error("bleak_retry_connector not available during config flow check")
            return False

        ble_device = async_ble_device_from_address(self.hass, self._address, connectable=True)
        if not ble_device:
            _LOGGER.debug("Config flow: device not reachable at %s", self._address)
            return False

        client: BleakClient | None = None
        try:
            _LOGGER.debug("Config flow: attempting connection to %s", self._address)
            async with asyncio.timeout(10):
                client = await establish_connection(
                    BleakClient,
                    device=ble_device,
                    name="Ooni Config Check",
                    max_attempts=1,
                )
            if client.is_connected:
                _LOGGER.debug("Config flow: connection successful")
                return True
            return False
        except Exception as err:
            _LOGGER.debug("Config flow: connection check failed: %s", err)
            return False
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
