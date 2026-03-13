from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

BINARY_SENSORS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="status_connected",
        name="Bluetooth Connection",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="probe_p1_connected",
        name="Probe 1 Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="probe_p2_connected",
        name="Probe 2 Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="eco_mode",
        name="Eco Mode",
        device_class=BinarySensorDeviceClass.POWER,
    ),
)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up binary sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        OoniBinarySensor(coordinator, description)
        for description in BINARY_SENSORS
    )

class OoniBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Represents a binary (on/off) sensor."""

    def __init__(self, coordinator, description):
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.address)},
            "name": coordinator.device_name,
            "manufacturer": "Ooni",
        }

    @property
    def is_on(self) -> bool:
        """Return whether the sensor is on or off."""

        # Special case: the connection sensor itself
        if self.entity_description.key == "status_connected":
            return self.coordinator.is_connected

        # All other sensors (probes, eco mode): return None when no data is available
        if not self.coordinator.data:
            return None

        return getattr(self.coordinator.data, self.entity_description.key, False)

    @property
    def available(self) -> bool:
        """Return whether the sensor is available."""
        # The connection sensor is always available (it represents the connection state itself)
        if self.entity_description.key == "status_connected":
            return True

        # All other sensors are only available when data has been received
        return self.coordinator.last_update_success
