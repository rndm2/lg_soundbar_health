"""Binary sensors for LG Soundbars Health."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HealthState
from .const import DATA_COORDINATOR, DOMAIN
from .entity import LGSoundbarHealthEntity


class HealthBinarySensorKey(StrEnum):
    """Health binary sensor keys."""

    CONNECTION = "connection"
    INITIAL_IP_CONNECTION = "initial_ip_connection"
    IP_CHANGED = "ip_changed"


@dataclass(frozen=True, kw_only=True)
class HealthBinarySensorDescription(BinarySensorEntityDescription):
    """Description for an LG Soundbar health binary sensor."""

    value_fn: Callable[[HealthState], bool | None]


BINARY_SENSOR_DESCRIPTIONS: tuple[HealthBinarySensorDescription, ...] = (
    HealthBinarySensorDescription(
        key=HealthBinarySensorKey.CONNECTION,
        name="Connection",
        icon="mdi:speaker-wireless",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda state: state.connected,
    ),
    HealthBinarySensorDescription(
        key=HealthBinarySensorKey.INITIAL_IP_CONNECTION,
        name="Initial IP connection",
        icon="mdi:ip-network-outline",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda state: state.initial_ip_connected,
    ),
    HealthBinarySensorDescription(
        key=HealthBinarySensorKey.IP_CHANGED,
        name="IP changed",
        icon="mdi:swap-horizontal",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda state: state.ip_changed,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LG Soundbars Health binary sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    known_ids: set[str] = set()

    def add_new_entities() -> None:
        new_ids = set(coordinator.data) - known_ids
        if not new_ids:
            return
        known_ids.update(new_ids)
        async_add_entities(
            LGSoundbarHealthBinarySensor(coordinator, source_entry_id, description)
            for source_entry_id in sorted(new_ids)
            for description in BINARY_SENSOR_DESCRIPTIONS
        )

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class LGSoundbarHealthBinarySensor(LGSoundbarHealthEntity, BinarySensorEntity):
    """Binary diagnostic sensor for an LG Soundbar."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    entity_description: HealthBinarySensorDescription

    def __init__(
        self,
        coordinator,
        source_entry_id: str,
        description: HealthBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, source_entry_id)
        self.entity_description = description
        self._attr_unique_id = f"lg_soundbar_health_{source_entry_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return binary sensor value."""
        state = self.health_state
        if state is None:
            return None
        return self.entity_description.value_fn(state)
