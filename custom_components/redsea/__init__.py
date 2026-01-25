"""Red Sea ReefBeat integration.

This module is responsible for:
- Creating the right coordinator for each config entry (cloud or local devices)
- Storing coordinators in `hass.data[DOMAIN]`
- Forwarding config entries to platform(s)
- Registering integration services
"""

from __future__ import annotations

import json  # pyright: ignore[reportUnusedImport]  # noqa: F401
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONFIG_FLOW_CLOUD_USERNAME,
    CONFIG_FLOW_HW_MODEL,
    CONFIG_FLOW_IP_ADDRESS,
    DOMAIN,
    HW_ATO_IDS,
    HW_DOSE_IDS,
    HW_G1_LED_IDS,
    HW_G2_LED_IDS,
    HW_MAT_IDS,
    HW_RUN_IDS,
    HW_WAVE_IDS,
    PLATFORMS,
    VIRTUAL_LED,
)
from .coordinator import (
    ReefATOCoordinator,
    ReefBeatCloudCoordinator,
    ReefBeatCoordinator,
    ReefDoseCoordinator,
    ReefLedCoordinator,
    ReefLedG2Coordinator,
    ReefMatCoordinator,
    ReefRunCoordinator,
    ReefVirtualLedCoordinator,
    ReefWaveCoordinator,
)

_LOGGER = logging.getLogger(__name__)


_HEAD_NAME_RE = re.compile(r"^(?P<prefix>.+)_head_(?P<head>\d+)$")


async def _migrate_head_device_names(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Migrate ReefDose per-head device display names.

    This only updates the registry `name` when the user hasn't set a custom name.
    """
    device_registry = dr.async_get(hass)
    for device_entry in dr.async_entries_for_config_entry(
        device_registry, entry.entry_id
    ):
        name = getattr(device_entry, "name", None)
        if not isinstance(name, str) or "_head_" not in name:
            continue

        # Don't override user customizations.
        if getattr(device_entry, "name_by_user", None):
            continue

        match = _HEAD_NAME_RE.match(name)
        if not match:
            continue

        new_name = f"{match.group('prefix')} head {match.group('head')}"
        if new_name == name:
            continue

        _LOGGER.debug(
            "Migrating head device name: %s -> %s (device_id=%s)",
            name,
            new_name,
            device_entry.id,
        )
        device_registry.async_update_device(device_entry.id, name=new_name)


async def _migrate_serial_numbers(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Backfill device registry serial numbers when available.

    Home Assistant does not necessarily update all device fields for already-existing
    devices on every startup. When we learn a serial number (via `/description.xml`),
    populate it for devices created previously.
    """
    # Cloud account entries do not have `/description.xml` and therefore no stable
    # device serial numbers to backfill.
    if CONFIG_FLOW_CLOUD_USERNAME in entry.data:
        return

    device_registry = dr.async_get(hass)
    device_entries = list(
        dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    )
    missing_serial = [
        d for d in device_entries if not getattr(d, "serial_number", None)
    ]
    if not missing_serial:
        return

    _LOGGER.debug(
        "Migrating serial numbers for entry_id=%s (missing=%s)",
        entry.entry_id,
        len(missing_serial),
    )
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        _LOGGER.debug("Coordinator is NONE for %s", entry.entry_id)
        return

    # Serial number fetch is best-effort and happens asynchronously during
    # coordinator setup. Wait briefly so backfill can occur after devices exist.
    wait_for_sn = getattr(coordinator, "async_wait_for_serial_number", None)
    if callable(wait_for_sn):
        with suppress(Exception):
            await cast(Callable[[float], Awaitable[None]], wait_for_sn)(3.0)

    serial_number = getattr(coordinator, "serial_number", None)
    if not isinstance(serial_number, str) or not serial_number:
        _LOGGER.debug("Serial number is NONE for %s", entry.entry_id)
        return

    _LOGGER.debug(
        "Backfilling serial number '%s' for entry_id=%s", serial_number, entry.entry_id
    )
    for device_entry in missing_serial:
        if getattr(device_entry, "serial_number", None):
            continue
        device_registry.async_update_device(
            device_entry.id, serial_number=serial_number
        )


# =============================================================================
# Helpers
# =============================================================================


def _build_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> ReefBeatCoordinator:
    """Create the correct coordinator for a local (non-cloud) device entry.

    Raises:
        ValueError: If the hardware model / configuration is unsupported.
    """
    ip = entry.data[CONFIG_FLOW_IP_ADDRESS]
    hw = entry.data[CONFIG_FLOW_HW_MODEL]

    if isinstance(ip, str) and ip.startswith(VIRTUAL_LED):
        return ReefVirtualLedCoordinator(hass, entry)

    if hw in HW_G1_LED_IDS:
        return ReefLedCoordinator(hass, entry)
    if hw in HW_G2_LED_IDS:
        return ReefLedG2Coordinator(hass, entry)
    if hw in HW_DOSE_IDS:
        return ReefDoseCoordinator(hass, entry)
    if hw in HW_MAT_IDS:
        return ReefMatCoordinator(hass, entry)
    if hw in HW_ATO_IDS:
        return ReefATOCoordinator(hass, entry)
    if hw in HW_RUN_IDS:
        return ReefRunCoordinator(hass, entry)
    if hw in HW_WAVE_IDS:
        return ReefWaveCoordinator(hass, entry)

    raise ValueError(f"Unsupported hardware model or configuration: ip={ip} hw={hw}")


# Config entry lifecycle
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry.

    Creates a coordinator, runs its setup, stores it into hass.data, and forwards
    entity platforms.
    """
    _LOGGER.debug(
        "async_setup_entry: entry_id='%s', data='%s'",
        entry.entry_id,
        entry.data,
    )

    try:
        if CONFIG_FLOW_CLOUD_USERNAME in entry.data:
            coordinator: ReefBeatCloudCoordinator | ReefBeatCoordinator = (
                ReefBeatCloudCoordinator(hass, entry)
            )
        else:
            coordinator = _build_coordinator(hass, entry)
    except Exception:
        _LOGGER.exception(
            "Failed to create coordinator for entry_id=%s", entry.entry_id
        )
        return False

    # Store early to avoid races where coordinators fire bus events during
    # `async_setup()` (e.g., cloud-link requests) before we populate hass.data.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    try:
        await coordinator.async_setup()
    except Exception:
        _LOGGER.exception("Failed to setup coordinator for entry_id=%s", entry.entry_id)
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        return False

    # Perform a single first refresh up-front so platform setup doesn't block on
    # `async_add_entities(..., update_before_add=True)` refreshes (which can trigger
    # repeated 10s platform setup warnings when many entities exist).
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        raise ConfigEntryNotReady(
            f"Initial refresh failed for entry_id={entry.entry_id}: {err}"
        ) from err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Best-effort cosmetic migration; doesn't affect identifiers or entities.
    with suppress(Exception):
        await _migrate_head_device_names(hass, entry)

    # Best-effort metadata backfill for existing devices.
    with suppress(Exception):
        await _migrate_serial_numbers(hass, entry)

    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
    if coordinator is not None:
        # coordinator may provide unload() (best-effort)
        with suppress(Exception):
            coordinator.unload()

    return True


# Services
async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (register services)."""

    @callback
    async def handle_request(call: ServiceCall) -> ServiceResponse:
        """Handle the `redsea.request` service call."""
        device_id = call.data.get("device_id")
        if not isinstance(device_id, str):
            return {"error": "Invalid device_id"}

        device = hass.data.get(DOMAIN, {}).get(device_id)
        if device is None:
            return {"error": "Device not enabled"}

        access_path = call.data.get("access_path")
        method = call.data.get("method")

        if not isinstance(access_path, str) or not isinstance(method, str):
            return {"error": "Invalid access_path or method"}

        try:
            if method == "get":
                r = await device.my_api.http_get(access_path)
            else:
                data: Any = call.data.get("data")
                _LOGGER.debug(
                    "Service request: device_id=%s path=%s method=%s data=%s",
                    device_id,
                    access_path,
                    method,
                    data,
                )
                r = await device.my_api.http_send(access_path, data, method)
        except Exception:
            _LOGGER.exception(
                "Service request failed: device_id=%s path=%s method=%s",
                device_id,
                access_path,
                method,
            )
            return {"error": "request failed"}

        _LOGGER.debug("REQUEST RESPONSE %s", r)

        if not r:
            title = getattr(device, "title", getattr(device, "_title", device_id))
            return {"error": f"can not access to device {title}"}

        # Debug-friendly structured response (matches reefbeat.api.HttpResult)
        resp: dict[str, Any] = {
            "ok": bool(r.get("ok")),
            "status": int(r.get("status", 0)),
            "reason": str(r.get("reason", "")),
            "method": str(r.get("method", "")),
            "url": str(r.get("url", "")),
            "elapsed_ms": int(r.get("elapsed_ms", 0)),
            "headers": dict(r.get("headers", {})),
        }

        if "json" in r:
            resp["json"] = r.get("json")
        else:
            resp["text"] = r.get("text", "")

        return resp

    _LOGGER.debug("Registering service redsea.request")
    hass.services.async_register(
        DOMAIN, "request", handle_request, supports_response=SupportsResponse.OPTIONAL
    )
    return True
