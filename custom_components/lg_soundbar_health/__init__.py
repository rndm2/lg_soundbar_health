"""LG Soundbars Health custom integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import socket
import time
from typing import Any

from homeassistant.components.persistent_notification import DOMAIN as NOTIFY_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.storage import Store

from .const import (
    DATA_COORDINATOR,
    CONF_AUTO_RELOAD,
    CONF_NOTIFY_ON_RELOAD,
    DEFAULT_AUTO_RELOAD,
    DEFAULT_AUTO_RELOAD_INITIAL_FAILURES,
    DEFAULT_NOTIFY_ON_RELOAD,
    DEFAULT_PARENT_RELOAD_COOLDOWN,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    CONF_SCAN_INTERVAL_SECONDS,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    SOURCE_DOMAIN,
    STORAGE_AUTO_RELOAD_ENABLED,
    STORAGE_INITIAL_ENDPOINTS,
    STORAGE_PARENT_RELOADS,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

MAX_STATE_LENGTH = 255


def ha_safe_text(value: Any, *, fallback: str | None = None) -> str | None:
    """Return a Home Assistant state-safe text value.

    Sensors states must be short scalar values. If a callable/function or other
    accidental object reaches a text field, expose unknown instead of its repr.
    """
    if value is None:
        return fallback
    if callable(value):
        return fallback
    if not isinstance(value, str):
        return fallback
    if value == "":
        return fallback
    if len(value) <= MAX_STATE_LENGTH:
        return value
    return f"{value[: MAX_STATE_LENGTH - 1]}…"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
]

LGSoundbarHealthConfigEntry = ConfigEntry


@dataclass(frozen=True, slots=True)
class SoundbarTarget:
    """A target discovered from an existing LG Soundbar config entry."""

    source_entry_id: str
    name: str
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    """Resolved network endpoint used for a TCP health check."""

    ip_address: str
    family: socket.AddressFamily


@dataclass(slots=True)
class InitialEndpoint:
    """Frozen IP snapshot for one source LG Soundbar config entry."""

    host: str
    port: int
    ip_address: str | None = None
    family: socket.AddressFamily | None = None
    captured_at: datetime | None = None
    error: str | None = None


@dataclass(slots=True)
class HealthState:
    """Runtime health state for one soundbar."""

    target: SoundbarTarget
    connected: bool = False
    response_time_ms: float | None = None
    checked_at: datetime | None = None
    last_success: datetime | None = None
    last_failure: datetime | None = None
    offline_since: datetime | None = None
    failure_count: int = 0
    last_error: str | None = None
    resolved_ip: str | None = None
    initial_ip: str | None = None
    initial_ip_captured_at: datetime | None = None
    initial_ip_error: str | None = None
    initial_ip_connected: bool | None = None
    initial_ip_response_time_ms: float | None = None
    initial_ip_last_error: str | None = None
    initial_ip_failure_count: int = 0
    auto_reload_enabled: bool = False
    last_parent_reload: datetime | None = None
    parent_reload_count: int = 0
    parent_reload_last_error: str | None = None
    parent_reload_in_progress: bool = False
    parent_reload_last_reason: str | None = None

    @property
    def offline_duration_seconds(self) -> int:
        """Return current offline duration in seconds."""
        if self.connected or self.offline_since is None:
            return 0
        return max(0, int((datetime.now(UTC) - self.offline_since).total_seconds()))

    @property
    def ip_changed(self) -> bool | None:
        """Return whether current DNS resolution differs from the initial IP."""
        if self.initial_ip is None or self.resolved_ip is None:
            return None
        return self.initial_ip != self.resolved_ip

    @property
    def auto_reload_ready(self) -> bool:
        """Return whether the current state satisfies the auto-reload condition."""
        return (
            self.auto_reload_enabled
            and self.ip_changed is True
            and self.connected is True
            and self.initial_ip_connected is False
            and self.initial_ip_failure_count >= DEFAULT_AUTO_RELOAD_INITIAL_FAILURES
            and not self.parent_reload_in_progress
        )


class LGSoundbarHealthCoordinator(DataUpdateCoordinator[dict[str, HealthState]]):
    """Coordinator that checks TCP reachability for existing LG soundbars."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        scan_interval_seconds = _parse_scan_interval_seconds(
            config_entry.options.get(CONF_SCAN_INTERVAL_SECONDS),
            DEFAULT_SCAN_INTERVAL_SECONDS,
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval_seconds),
        )
        self._config_entry = config_entry
        self._states: dict[str, HealthState] = {}
        self._initial_endpoints: dict[str, InitialEndpoint] = {}
        self._source_state_unsub: dict[str, Any] = {}
        self._parent_reload_tasks: set[asyncio.Task[Any]] = set()
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._parent_reload_history: dict[str, dict[str, Any]] = {}

    @property
    def auto_reload_enabled(self) -> bool:
        """Return whether guarded automatic parent reload is enabled."""
        return bool(
            self._config_entry.options.get(CONF_AUTO_RELOAD, DEFAULT_AUTO_RELOAD)
        )

    @property
    def notify_on_reload(self) -> bool:
        """Return whether reloads should create persistent notifications."""
        return bool(
            self._config_entry.options.get(
                CONF_NOTIFY_ON_RELOAD, DEFAULT_NOTIFY_ON_RELOAD
            )
        )

    async def async_load_storage(self) -> None:
        """Load persisted per-source settings and diagnostic history."""
        stored = await self._store.async_load() or {}

        self._initial_endpoints = _deserialize_initial_endpoints(
            stored.get(STORAGE_INITIAL_ENDPOINTS, {})
        )
        self._parent_reload_history = _deserialize_parent_reload_history(
            stored.get(STORAGE_PARENT_RELOADS, {})
        )

    async def _async_save_storage(self) -> None:
        """Persist per-source settings and diagnostic history."""
        await self._store.async_save(
            {
                STORAGE_INITIAL_ENDPOINTS: _serialize_initial_endpoints(self._initial_endpoints),
                STORAGE_PARENT_RELOADS: dict(self._parent_reload_history),
            }
        )

    def discover_targets(self) -> list[SoundbarTarget]:
        """Discover LG Soundbar config entries and turn them into health targets."""
        targets: list[SoundbarTarget] = []

        for source_entry in self.hass.config_entries.async_entries(SOURCE_DOMAIN):
            host = str(source_entry.data.get(CONF_HOST, "")).strip()
            if not host:
                _LOGGER.debug(
                    "Skipping LG Soundbar entry %s because it has no host",
                    source_entry.entry_id,
                )
                continue

            self._ensure_source_state_listener(source_entry)

            port = _parse_port(source_entry.data.get(CONF_PORT), DEFAULT_PORT)
            name = str(source_entry.title or source_entry.data.get(CONF_NAME) or host)

            targets.append(
                SoundbarTarget(
                    source_entry_id=source_entry.entry_id,
                    name=name,
                    host=host,
                    port=port,
                )
            )

        return targets

    @callback
    def _ensure_source_state_listener(self, source_entry: ConfigEntry) -> None:
        """Track source entry reloads so the frozen initial IP can be refreshed."""
        if source_entry.entry_id in self._source_state_unsub:
            return

        @callback
        def _source_state_changed(*_: Any) -> None:
            if source_entry.state is ConfigEntryState.LOADED:
                _LOGGER.debug(
                    "LG Soundbar source entry %s loaded; refreshing initial IP snapshot",
                    source_entry.entry_id,
                )
                self._initial_endpoints.pop(source_entry.entry_id, None)
                self.hass.async_create_task(self._async_save_storage())
                self.hass.async_create_task(self.async_request_refresh())

        self._source_state_unsub[source_entry.entry_id] = source_entry.async_on_state_change(
            _source_state_changed
        )

    async def _async_update_data(self) -> dict[str, HealthState]:
        """Check all discovered soundbars."""
        targets = self.discover_targets()
        current_ids = {target.source_entry_id for target in targets}

        for stale_id in set(self._states) - current_ids:
            self._states.pop(stale_id, None)
            self._initial_endpoints.pop(stale_id, None)
            self._parent_reload_history.pop(stale_id, None)
            self.hass.async_create_task(self._async_save_storage())
            if unsub := self._source_state_unsub.pop(stale_id, None):
                unsub()

        if targets:
            await asyncio.gather(*(self._check_target(target) for target in targets))

        return dict(self._states)

    async def _check_target(self, target: SoundbarTarget) -> None:
        """Check one target by resolving its host and opening TCP connections."""
        state = self._states.setdefault(target.source_entry_id, HealthState(target=target))
        state.target = target
        state.auto_reload_enabled = self.auto_reload_enabled
        if not state.parent_reload_in_progress:
            self._apply_persisted_parent_reload_state(state)

        checked_at = datetime.now(UTC)
        current_endpoint: ResolvedEndpoint | None = None

        try:
            state.resolved_ip = None
            started = time.perf_counter()
            current_endpoint = await asyncio.wait_for(
                self._resolve_host(target.host, target.port), timeout=DEFAULT_TIMEOUT
            )
            state.resolved_ip = current_endpoint.ip_address
            await self._open_tcp_connection(current_endpoint.ip_address, target.port, current_endpoint.family)
        except Exception as err:  # noqa: BLE001 - a health check must not break polling
            _mark_failure(state, checked_at, err)
        else:
            state.connected = True
            state.response_time_ms = round((time.perf_counter() - started) * 1000, 1)
            state.checked_at = checked_at
            state.last_success = checked_at
            state.last_error = None
            state.failure_count = 0
            state.offline_since = None

        initial_endpoint = await self._async_get_initial_endpoint(target, current_endpoint, checked_at)
        state.initial_ip = initial_endpoint.ip_address
        state.initial_ip_captured_at = initial_endpoint.captured_at
        state.initial_ip_error = initial_endpoint.error

        if initial_endpoint.ip_address is None or initial_endpoint.family is None:
            state.initial_ip_connected = None
            state.initial_ip_response_time_ms = None
            state.initial_ip_last_error = initial_endpoint.error
            state.initial_ip_failure_count = 0
            return

        try:
            started = time.perf_counter()
            await self._open_tcp_connection(
                initial_endpoint.ip_address,
                initial_endpoint.port,
                initial_endpoint.family,
            )
        except Exception as err:  # noqa: BLE001 - diagnostic only
            state.initial_ip_connected = False
            state.initial_ip_response_time_ms = None
            state.initial_ip_last_error = _format_error(err)
            state.initial_ip_failure_count += 1
        else:
            state.initial_ip_connected = True
            state.initial_ip_response_time_ms = round((time.perf_counter() - started) * 1000, 1)
            state.initial_ip_last_error = None
            state.initial_ip_failure_count = 0

        self._maybe_schedule_auto_reload(state)

    async def _async_get_initial_endpoint(
        self,
        target: SoundbarTarget,
        current_endpoint: ResolvedEndpoint | None,
        checked_at: datetime,
    ) -> InitialEndpoint:
        """Return or create the frozen initial IP snapshot for a target."""
        existing = self._initial_endpoints.get(target.source_entry_id)
        if existing is not None and existing.host == target.host and existing.port == target.port:
            if existing.ip_address is not None:
                return existing
            # Retry after a failed initial resolution, but do not overwrite a
            # successful snapshot until the source entry is reloaded or host/port changes.

        if current_endpoint is not None:
            endpoint = InitialEndpoint(
                host=target.host,
                port=target.port,
                ip_address=current_endpoint.ip_address,
                family=current_endpoint.family,
                captured_at=checked_at,
                error=None,
            )
            self._initial_endpoints[target.source_entry_id] = endpoint
            await self._async_save_storage()
            return endpoint

        try:
            resolved = await asyncio.wait_for(
                self._resolve_host(target.host, target.port), timeout=DEFAULT_TIMEOUT
            )
        except Exception as err:  # noqa: BLE001 - diagnostic only
            endpoint = InitialEndpoint(
                host=target.host,
                port=target.port,
                ip_address=None,
                family=None,
                captured_at=checked_at,
                error=_format_error(err),
            )
            self._initial_endpoints[target.source_entry_id] = endpoint
            await self._async_save_storage()
            return endpoint

        endpoint = InitialEndpoint(
            host=target.host,
            port=target.port,
            ip_address=resolved.ip_address,
            family=resolved.family,
            captured_at=checked_at,
            error=None,
        )
        self._initial_endpoints[target.source_entry_id] = endpoint
        await self._async_save_storage()
        return endpoint

    async def async_reset_initial_endpoint(self, source_entry_id: str) -> None:
        """Reset the frozen initial IP snapshot for one source entry."""
        self._initial_endpoints.pop(source_entry_id, None)
        await self._async_save_storage()
        await self.async_request_refresh()

    async def async_reload_parent(self, source_entry_id: str, reason: str = "manual") -> None:
        """Reload the parent LG Soundbar config entry."""
        state = self._states.get(source_entry_id)
        if state is None:
            return

        if state.parent_reload_in_progress:
            _LOGGER.debug(
                "Skipping LG Soundbar parent reload for %s because a reload is already in progress",
                source_entry_id,
            )
            return

        source_entry = self._get_source_entry(source_entry_id)
        if source_entry is None:
            state.parent_reload_last_error = "Source LG Soundbar config entry not found"
            self._persist_parent_reload_state(state)
            self.async_set_updated_data(dict(self._states))
            await self._async_save_storage()
            return

        state.parent_reload_in_progress = True
        state.parent_reload_last_reason = ha_safe_text(reason, fallback="unknown") or "unknown"
        state.parent_reload_last_error = None
        state.last_parent_reload = datetime.now(UTC)
        state.parent_reload_count += 1
        self._persist_parent_reload_state(state)
        self.async_set_updated_data(dict(self._states))
        await self._async_save_storage()

        reload_succeeded = False
        try:
            reload_result = await self.hass.config_entries.async_reload(source_entry.entry_id)
        except Exception as err:  # noqa: BLE001 - reload failures must stay diagnostic
            state.parent_reload_last_error = _format_error(err)
            _LOGGER.warning(
                "Failed to reload LG Soundbar source entry %s: %s",
                source_entry.entry_id,
                err,
            )
        else:
            if reload_result is False:
                state.parent_reload_last_error = "Home Assistant returned false while reloading source config entry"
            else:
                reload_succeeded = True
                state.parent_reload_last_error = None
                self._initial_endpoints.pop(source_entry.entry_id, None)
        finally:
            if self.notify_on_reload:
                await self._async_create_reload_notification(state, reload_succeeded)
            state.parent_reload_in_progress = False
            self._persist_parent_reload_state(state)
            self.async_set_updated_data(dict(self._states))
            await self._async_save_storage()
            await self.async_request_refresh()

    async def _async_create_reload_notification(
        self,
        state: HealthState,
        reload_succeeded: bool,
    ) -> None:
        """Create a persistent notification for a parent reload attempt."""
        status = "succeeded" if reload_succeeded else "failed"
        message = (
            f"Reload of LG Soundbars config entry for {state.target.name} {status}.\n\n"
            f"Reason: {state.parent_reload_last_reason or 'unknown'}\n"
            f"Initial IP: {state.initial_ip or 'unknown'}\n"
            f"Resolved IP: {state.resolved_ip or 'unknown'}"
        )
        if state.parent_reload_last_error:
            message += f"\nError: {state.parent_reload_last_error}"

        await self.hass.services.async_call(
            NOTIFY_DOMAIN,
            "create",
            {
                "title": "LG Soundbars Health",
                "message": message,
                "notification_id": (
                    f"lg_soundbar_health_reload_{state.target.source_entry_id}"
                ),
            },
            blocking=False,
        )

    def _apply_persisted_parent_reload_state(self, state: HealthState) -> None:
        """Restore parent reload history into a runtime state."""
        raw = self._parent_reload_history.get(state.target.source_entry_id)
        if not raw:
            state.parent_reload_last_reason = state.parent_reload_last_reason or "never"
            return

        state.last_parent_reload = _parse_datetime(raw.get("last_parent_reload"))
        state.parent_reload_count = _parse_non_negative_int(raw.get("parent_reload_count"), 0)
        state.parent_reload_last_reason = (
            ha_safe_text(raw.get("parent_reload_last_reason"), fallback="never") or "never"
        )
        state.parent_reload_last_error = ha_safe_text(raw.get("parent_reload_last_error"))

    def _persist_parent_reload_state(self, state: HealthState) -> None:
        """Store parent reload history for a source entry."""
        self._parent_reload_history[state.target.source_entry_id] = {
            "last_parent_reload": _datetime_to_storage(state.last_parent_reload),
            "parent_reload_count": state.parent_reload_count,
            "parent_reload_last_reason": (
                ha_safe_text(state.parent_reload_last_reason, fallback="never") or "never"
            ),
            "parent_reload_last_error": ha_safe_text(state.parent_reload_last_error),
        }

    def _maybe_schedule_auto_reload(self, state: HealthState) -> None:
        """Schedule a parent reload if the guarded auto-reload condition is met."""
        if not state.auto_reload_ready:
            return

        now = datetime.now(UTC)
        if state.last_parent_reload is not None:
            elapsed = now - state.last_parent_reload
            if elapsed < DEFAULT_PARENT_RELOAD_COOLDOWN:
                return

        task = self.hass.async_create_task(
            self.async_reload_parent(state.target.source_entry_id, reason="auto_ip_changed")
        )
        self._parent_reload_tasks.add(task)
        task.add_done_callback(self._parent_reload_tasks.discard)

    def _get_source_entry(self, source_entry_id: str) -> ConfigEntry | None:
        """Return a source LG Soundbar config entry by entry id."""
        for source_entry in self.hass.config_entries.async_entries(SOURCE_DOMAIN):
            if source_entry.entry_id == source_entry_id:
                return source_entry
        return None

    async def _open_tcp_connection(
        self,
        ip_address: str,
        port: int,
        family: socket.AddressFamily,
    ) -> None:
        """Open and immediately close a TCP connection to an endpoint."""
        writer: asyncio.StreamWriter | None = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host=ip_address, port=port, family=family),
                timeout=DEFAULT_TIMEOUT,
            )
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=DEFAULT_TIMEOUT)
                except Exception as err:  # noqa: BLE001 - close errors are diagnostic only
                    _LOGGER.debug(
                        "Error while closing LG Soundbars Health socket for %s:%s: %s",
                        ip_address,
                        port,
                        err,
                    )

    async def _resolve_host(self, host: str, port: int) -> ResolvedEndpoint:
        """Resolve host to an IP address.

        Current resolution is done on every health check so DHCP/DNS changes are visible.
        IPv4 is preferred because LG soundbars on home LANs normally expose control
        ports on IPv4. IPv6 is still supported when no IPv4 address is returned.
        """
        addr_info = await self.hass.async_add_executor_job(
            socket.getaddrinfo,
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
        if not addr_info:
            raise OSError(f"Could not resolve host: {host}")

        for family, _type, _proto, _canonname, sockaddr in addr_info:
            if family == socket.AF_INET:
                return ResolvedEndpoint(ip_address=str(sockaddr[0]), family=family)

        family, _type, _proto, _canonname, sockaddr = addr_info[0]
        return ResolvedEndpoint(ip_address=str(sockaddr[0]), family=family)

    def async_shutdown(self) -> None:
        """Unsubscribe from source entry listeners."""
        for unsub in self._source_state_unsub.values():
            unsub()
        self._source_state_unsub.clear()
        for task in self._parent_reload_tasks:
            task.cancel()
        self._parent_reload_tasks.clear()



async def _async_ensure_default_options(
    hass: HomeAssistant,
    entry: LGSoundbarHealthConfigEntry,
) -> None:
    """Ensure options exist and migrate the old auto-reload storage flag.

    Version 1.8.2 moved auto reload from a switch entity into config entry
    options. Existing installs can still have the old per-source storage map.
    This migration must be best-effort and must never block integration setup.
    """
    current_options = dict(entry.options)

    stored_auto_reload = DEFAULT_AUTO_RELOAD
    if CONF_AUTO_RELOAD not in current_options:
        try:
            stored = await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_load() or {}
        except Exception as err:  # noqa: BLE001 - migration must not break setup
            _LOGGER.debug(
                "Could not load LG Soundbars Health storage while migrating options: %s",
                err,
            )
            stored = {}

        raw_auto_reload = stored.get(STORAGE_AUTO_RELOAD_ENABLED, {})
        if isinstance(raw_auto_reload, dict):
            stored_auto_reload = any(bool(value) for value in raw_auto_reload.values())
        elif isinstance(raw_auto_reload, bool):
            stored_auto_reload = raw_auto_reload

    updated_options = {
        CONF_AUTO_RELOAD: bool(
            current_options.get(CONF_AUTO_RELOAD, stored_auto_reload)
        ),
        CONF_NOTIFY_ON_RELOAD: bool(
            current_options.get(CONF_NOTIFY_ON_RELOAD, DEFAULT_NOTIFY_ON_RELOAD)
        ),
        CONF_SCAN_INTERVAL_SECONDS: _parse_scan_interval_seconds(
            current_options.get(
                CONF_SCAN_INTERVAL_SECONDS,
                DEFAULT_SCAN_INTERVAL_SECONDS,
            ),
            DEFAULT_SCAN_INTERVAL_SECONDS,
        ),
    }

    if updated_options != current_options:
        hass.config_entries.async_update_entry(entry, options=updated_options)


async def async_setup_entry(hass: HomeAssistant, entry: LGSoundbarHealthConfigEntry) -> bool:
    """Set up LG Soundbars Health from a config entry."""
    await _async_ensure_default_options(hass, entry)
    coordinator = LGSoundbarHealthCoordinator(hass, entry)
    await coordinator.async_load_storage()
    await coordinator.async_config_entry_first_refresh()

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DATA_COORDINATOR: coordinator}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: LGSoundbarHealthConfigEntry) -> None:
    """Reload the integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LGSoundbarHealthConfigEntry) -> bool:
    """Unload LG Soundbars Health."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if data and (coordinator := data.get(DATA_COORDINATOR)):
            coordinator.async_shutdown()
    return unload_ok


def _serialize_initial_endpoints(endpoints: dict[str, InitialEndpoint]) -> dict[str, dict[str, Any]]:
    """Serialize initial endpoints for Home Assistant storage."""
    data: dict[str, dict[str, Any]] = {}
    for source_entry_id, endpoint in endpoints.items():
        data[source_entry_id] = {
            "host": endpoint.host,
            "port": endpoint.port,
            "ip_address": endpoint.ip_address,
            "family": int(endpoint.family) if endpoint.family is not None else None,
            "captured_at": _datetime_to_storage(endpoint.captured_at),
            "error": ha_safe_text(endpoint.error),
        }
    return data


def _deserialize_initial_endpoints(raw: Any) -> dict[str, InitialEndpoint]:
    """Deserialize initial endpoints from Home Assistant storage."""
    if not isinstance(raw, dict):
        return {}

    endpoints: dict[str, InitialEndpoint] = {}
    for source_entry_id, payload in raw.items():
        if not source_entry_id or not isinstance(payload, dict):
            continue

        host = ha_safe_text(payload.get("host"))
        if host is None:
            continue

        port = _parse_port(payload.get("port"), DEFAULT_PORT)
        ip_address = ha_safe_text(payload.get("ip_address"))
        family = _parse_socket_family(payload.get("family"))
        if ip_address is None or family is None:
            ip_address = None
            family = None

        endpoints[str(source_entry_id)] = InitialEndpoint(
            host=host,
            port=port,
            ip_address=ip_address,
            family=family,
            captured_at=_parse_datetime(payload.get("captured_at")),
            error=ha_safe_text(payload.get("error")),
        )

    return endpoints


def _deserialize_parent_reload_history(raw: Any) -> dict[str, dict[str, Any]]:
    """Deserialize parent reload history from Home Assistant storage."""
    if not isinstance(raw, dict):
        return {}

    history: dict[str, dict[str, Any]] = {}
    for source_entry_id, payload in raw.items():
        if not source_entry_id or not isinstance(payload, dict):
            continue

        history[str(source_entry_id)] = {
            "last_parent_reload": _datetime_to_storage(
                _parse_datetime(payload.get("last_parent_reload"))
            ),
            "parent_reload_count": _parse_non_negative_int(
                payload.get("parent_reload_count"), 0
            ),
            "parent_reload_last_reason": (
                ha_safe_text(payload.get("parent_reload_last_reason"), fallback="never")
                or "never"
            ),
            "parent_reload_last_error": ha_safe_text(payload.get("parent_reload_last_error")),
        }

    return history


def _parse_socket_family(raw_value: Any) -> socket.AddressFamily | None:
    """Parse a stored socket address family."""
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None

    try:
        family = socket.AddressFamily(value)
    except ValueError:
        return None

    if family in (socket.AF_INET, socket.AF_INET6):
        return family
    return None


def _parse_datetime(raw_value: Any) -> datetime | None:
    """Parse a stored ISO datetime."""
    if not isinstance(raw_value, str) or not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _datetime_to_storage(value: datetime | None) -> str | None:
    """Serialize a datetime for storage."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _parse_non_negative_int(raw_value: Any, fallback: int) -> int:
    """Parse a non-negative integer."""
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return fallback
    return max(0, value)


def _parse_scan_interval_seconds(raw_value: Any, fallback: int) -> int:
    """Parse and clamp the health check interval in seconds."""
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return fallback

    return min(MAX_SCAN_INTERVAL_SECONDS, max(MIN_SCAN_INTERVAL_SECONDS, value))


def _parse_port(raw_port: Any, fallback: int) -> int:
    """Parse and validate a TCP port."""
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        return fallback

    if 1 <= port <= 65535:
        return port
    return fallback


def _mark_failure(state: HealthState, checked_at: datetime, err: Exception) -> None:
    """Update state after a failed current-DNS health check."""
    state.connected = False
    state.response_time_ms = None
    state.checked_at = checked_at
    state.last_failure = checked_at
    state.failure_count += 1
    state.last_error = _format_error(err)
    if state.offline_since is None:
        state.offline_since = checked_at


def _format_error(err: Exception) -> str:
    """Return an HA-safe error string for use as a sensor state."""
    message = str(err) or err.__class__.__name__
    return ha_safe_text(message, fallback=err.__class__.__name__) or err.__class__.__name__
