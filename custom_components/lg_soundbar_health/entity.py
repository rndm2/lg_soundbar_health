"""Base entities for LG Soundbars Health."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HealthState, LGSoundbarHealthCoordinator, ha_safe_text
from .const import DOMAIN, SOURCE_DOMAIN


class LGSoundbarHealthEntity(CoordinatorEntity[LGSoundbarHealthCoordinator]):
    """Base entity for one LG soundbar health target."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LGSoundbarHealthCoordinator,
        source_entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._source_entry_id = source_entry_id
        self._linked_device_entry: DeviceEntry | None = self._find_source_device()
        if self._linked_device_entry is not None:
            self._attr_device_info = self._source_device_info(self._linked_device_entry)

    def _find_source_device(self) -> DeviceEntry | None:
        """Return the native LG Soundbar device for the source config entry."""
        device_reg = dr.async_get(self.coordinator.hass)
        source_devices = dr.async_entries_for_config_entry(device_reg, self._source_entry_id)
        if not source_devices:
            return None
        return source_devices[0]


    def _source_device_info(self, source_device: DeviceEntry) -> DeviceInfo:
        """Return device info that attaches helper entities to the native device.

        This intentionally mirrors the Ring helper approach: reuse the native
        device registry identifiers/connections so Home Assistant shows this
        helper integration on the same device page as the source integration.
        """
        device_info: DeviceInfo = {}
        if source_device.identifiers:
            device_info["identifiers"] = set(source_device.identifiers)
        if source_device.connections:
            device_info["connections"] = set(source_device.connections)
        if source_device.name_by_user or source_device.name:
            device_info["name"] = source_device.name_by_user or source_device.name
        if source_device.manufacturer:
            device_info["manufacturer"] = source_device.manufacturer
        if source_device.model:
            device_info["model"] = source_device.model
        if source_device.sw_version:
            device_info["sw_version"] = source_device.sw_version
        if source_device.hw_version:
            device_info["hw_version"] = source_device.hw_version
        if device_info.get("identifiers") or device_info.get("connections"):
            return device_info

        return DeviceInfo(
            identifiers={(DOMAIN, self._source_entry_id)},
            name=source_device.name_by_user or source_device.name or "LG Soundbars Health",
            manufacturer="LG",
            model="Soundbars health monitor",
        )

    @property
    def health_state(self) -> HealthState | None:
        """Return current health state for this source soundbar.

        Do not call this property ``state``: Home Assistant uses ``Entity.state``
        as the public entity state. Returning our HealthState dataclass there
        makes HA stringify the whole object and reject it because states are
        limited to 255 characters.
        """
        return self.coordinator.data.get(self._source_entry_id) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        """Keep health entities available while their source config entry exists."""
        return self.health_state is not None

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return fallback device info only if no native device is available."""
        if self._linked_device_entry is not None:
            return self._source_device_info(self._linked_device_entry)

        state = self.health_state
        if state is None:
            return None

        return DeviceInfo(
            identifiers={(DOMAIN, self._source_entry_id)},
            name=state.target.name,
            manufacturer="LG",
            model="Soundbars health monitor",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return common diagnostic attributes."""
        state = self.health_state
        if state is None:
            return {}

        return {
            "host": ha_safe_text(state.target.host),
            "initial_ip": state.initial_ip,
            "initial_ip_captured_at": (
                state.initial_ip_captured_at.isoformat()
                if state.initial_ip_captured_at
                else None
            ),
            "initial_ip_error": ha_safe_text(state.initial_ip_error),
            "initial_ip_connected": state.initial_ip_connected,
            "initial_ip_response_time_ms": state.initial_ip_response_time_ms,
            "initial_ip_last_error": ha_safe_text(state.initial_ip_last_error),
            "resolved_ip": state.resolved_ip,
            "ip_changed": state.ip_changed,
            "port": state.target.port,
            "checked_at": state.checked_at.isoformat() if state.checked_at else None,
            "last_success": state.last_success.isoformat() if state.last_success else None,
            "last_failure": state.last_failure.isoformat() if state.last_failure else None,
            "offline_since": state.offline_since.isoformat() if state.offline_since else None,
            "offline_duration_seconds": state.offline_duration_seconds,
            "failure_count": state.failure_count,
            "last_error": ha_safe_text(state.last_error),
            "response_time_ms": state.response_time_ms,
            "initial_ip_failure_count": state.initial_ip_failure_count,
            "auto_reload_enabled": state.auto_reload_enabled,
            "auto_reload_ready": state.auto_reload_ready,
            "last_parent_reload": (
                state.last_parent_reload.isoformat() if state.last_parent_reload else None
            ),
            "parent_reload_count": state.parent_reload_count,
            "parent_reload_last_error": ha_safe_text(state.parent_reload_last_error),
            "parent_reload_in_progress": state.parent_reload_in_progress,
            "parent_reload_last_reason": ha_safe_text(state.parent_reload_last_reason),
            "source_config_entry_id": ha_safe_text(state.target.source_entry_id),
            "source_integration": SOURCE_DOMAIN,
        }
