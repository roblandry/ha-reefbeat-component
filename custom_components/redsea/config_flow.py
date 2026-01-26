"""Config flow for the Red Sea ReefBeat integration.

Supports:
- Adding a ReefBeat Cloud account
- Auto-detecting local devices on the LAN
- Creating a virtual LED entry
- Options flow (scan interval, config mode, etc.)
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from functools import partial
from time import time
from typing import Any, cast

import async_timeout
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import network as ha_network
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auto_detect import ReefBeatInfo, get_reefbeats, get_unique_id, is_reefbeat
from .const import (
    ADD_CLOUD_API,
    ADD_LOCAL_DETECT,
    ADD_TYPES,
    ATO_SCAN_INTERVAL,
    CLOUD_DEVICE_TYPE,
    CLOUD_SCAN_INTERVAL,
    CLOUD_SERVER_ADDR,
    CONFIG_FLOW_ADD_TYPE,
    CONFIG_FLOW_CLOUD_PASSWORD,
    CONFIG_FLOW_CLOUD_USERNAME,
    CONFIG_FLOW_CONFIG_TYPE,
    CONFIG_FLOW_HW_MODEL,
    CONFIG_FLOW_INTENSITY_COMPENSATION,
    CONFIG_FLOW_IP_ADDRESS,
    CONFIG_FLOW_SCAN_INTERVAL,
    CONFIG_FLOW_SUBNETWORK,
    DOMAIN,
    DOSE_SCAN_INTERVAL,
    ENTER_IP,
    HTTP_DELAY_BETWEEN_RETRY,
    HTTP_MAX_RETRY,
    HW_ATO_IDS,
    HW_DOSE_IDS,
    HW_LED_IDS,
    HW_MAT_IDS,
    HW_RUN_IDS,
    LED_SCAN_INTERVAL,
    LEDS_INTENSITY_COMPENSATION,
    LINKED_LED,
    MAT_SCAN_INTERVAL,
    RUN_SCAN_INTERVAL,
    SCAN_INTERVAL,
    VIRTUAL_LED,
    VIRTUAL_LED_SCAN_INTERVAL,
)
from .reefbeat import parse

_LOGGER = logging.getLogger(__name__)


# Helpers
async def validate_cloud_input(
    hass: HomeAssistant, username: str, password: str
) -> bool:
    """Validate ReefBeat cloud credentials.

    Notes:
        Uses OAuth password grant against CLOUD_SERVER_ADDR.
    """
    _LOGGER.debug("Validating cloud credentials for user '%s'", username)

    headers = {
        "Authorization": "Basic Z0ZqSHRKcGE6Qzlmb2d3cmpEV09SVDJHWQ==",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    payload = {
        "grant_type": "password",
        "username": username,
        "password": password,
    }

    session = async_get_clientsession(hass)

    try:
        async with async_timeout.timeout(10):
            async with session.post(
                f"https://{CLOUD_SERVER_ADDR}/oauth/token",
                data=payload,
                headers=headers,
                ssl=False,
            ) as resp:
                status = int(resp.status)
    except Exception:
        _LOGGER.exception("Cloud credential validation failed due to request error")
        return False

    if status != 200:
        _LOGGER.warning("Cloud authentication failed (status=%s)", status)
        return False

    return True


# =============================================================================
# Helpers
# =============================================================================


def get_scan_interval(hw_model: str) -> int:
    """Return the default scan interval based on hardware model."""
    if hw_model in HW_DOSE_IDS:
        return DOSE_SCAN_INTERVAL
    if hw_model in HW_MAT_IDS:
        return MAT_SCAN_INTERVAL
    if hw_model in HW_ATO_IDS:
        return ATO_SCAN_INTERVAL
    if hw_model in HW_LED_IDS:
        return LED_SCAN_INTERVAL
    if hw_model in HW_RUN_IDS:
        return RUN_SCAN_INTERVAL
    if hw_model == CLOUD_DEVICE_TYPE:
        return CLOUD_SCAN_INTERVAL
    return SCAN_INTERVAL


def get_scan_interval_safe(hw_model: str | None) -> int:
    """Return scan interval for hw_model, defaulting safely when unknown/None."""
    if not hw_model:
        return SCAN_INTERVAL
    return get_scan_interval(hw_model)


def _is_cidr(address: str) -> bool:
    """Return True if the string looks like an IPv4 CIDR (e.g. 192.168.1.0/24).

    Note: A plain IP like "192.168.1.10" is not treated as a CIDR. This keeps
    manual IP entry working (probe a single device) while still allowing
    power-users to input an explicit CIDR to scan.
    """
    if "/" not in address:
        return False
    try:
        ipaddress.ip_network(address, strict=False)
        return True
    except Exception:
        return False


def _device_to_string(d: ReefBeatInfo) -> str:
    """Serialize a detected device into a selection string.

    ReefBeatInfo is a `TypedDict(total=False)`, so keys may be missing.
    """
    ip = d.get("ip", "")
    hw_model = d.get("hw_model", "")
    friendly_name = d.get("friendly_name", "")
    return f"{ip} {hw_model} {friendly_name}".strip()


async def _async_get_ipv4_subnet_choices(hass: HomeAssistant) -> list[str]:
    """Return IPv4 CIDR choices derived from HA's configured adapters.

    This is intentionally fast (no scanning), and is used only to decide whether
    we should ask the user which subnet to scan.
    """

    try:
        # Home Assistant provides async_get_adapters(). Some older versions have
        # get_adapters(); support both without importing version-specific types.
        ha_net_any: Any = cast(Any, ha_network)
        if hasattr(ha_net_any, "async_get_adapters"):
            adapters = await ha_net_any.async_get_adapters(hass)
        else:
            adapters = ha_net_any.get_adapters(hass)
    except Exception:  # pragma: no cover
        return []

    networks: set[str] = set()

    for adapter in adapters:
        ipv4_addrs: Any = getattr(adapter, "ipv4", None)
        if ipv4_addrs is None and isinstance(adapter, dict):
            ipv4_addrs = cast(dict[str, Any], adapter).get("ipv4")
        if not ipv4_addrs:
            continue

        for addr in cast(list[Any], ipv4_addrs):
            address: Any = getattr(addr, "address", None)
            prefix: Any = getattr(addr, "network_prefix", None)
            if address is None and isinstance(addr, dict):
                addr_dict = cast(dict[str, Any], addr)
                address = addr_dict.get("address")
                prefix = addr_dict.get("network_prefix")
            if not address or prefix is None:
                continue

            try:
                net = ipaddress.ip_network(
                    f"{str(address)}/{int(prefix)}",
                    strict=False,
                )
            except Exception:
                continue

            if not isinstance(net, ipaddress.IPv4Network):
                continue
            if net.is_loopback:
                continue

            networks.add(str(net))

    # Sort for stable UI.
    return sorted(networks, key=lambda s: ipaddress.ip_network(s).network_address)


# Config flow

# =============================================================================
# Classes
# =============================================================================


class ReefBeatConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """ReefBeat config flow."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        super().__init__()
        self._select_device_note: str | None = None

    async def _unique_id(self, user_input: dict[str, Any]) -> str:
        """Resolve device UUID for a local device entry (retrying as needed)."""
        ip = str(user_input[CONFIG_FLOW_IP_ADDRESS]).split(" ")[0]
        retry = HTTP_MAX_RETRY
        while retry > 0:
            uuid = await self.hass.async_add_executor_job(partial(get_unique_id, ip=ip))
            if uuid is not None:
                return str(uuid)
            retry -= 1
            _LOGGER.warning("Could not get UUID for %s, retrying...", ip)
            await asyncio.sleep(HTTP_DELAY_BETWEEN_RETRY)

        _LOGGER.error("Could not get UUID for %s; falling back to IP as unique_id", ip)
        return ip

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step and subsequent submissions."""
        # Step 1: choose add type
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONFIG_FLOW_ADD_TYPE, default=ADD_CLOUD_API
                        ): vol.In(ADD_TYPES)
                    }
                ),
            )

        # Step 2: branch by add type selection
        if CONFIG_FLOW_ADD_TYPE in user_input:
            add_type = user_input[CONFIG_FLOW_ADD_TYPE]

            if add_type == ADD_CLOUD_API:
                _LOGGER.info("Adding ReefBeat Cloud account")
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONFIG_FLOW_CLOUD_USERNAME): str,
                            vol.Required(CONFIG_FLOW_CLOUD_PASSWORD): str,
                        }
                    ),
                )

            if add_type == ADD_LOCAL_DETECT:
                # If HA has multiple IPv4 networks configured, ask which one to scan.
                subnet_choices = await _async_get_ipv4_subnet_choices(self.hass)
                if len(subnet_choices) > 1:
                    return await self.async_step_select_subnetwork()
                if len(subnet_choices) == 1:
                    self._subnetwork = subnet_choices[0]
                    return await self.auto_detect(self._subnetwork)
                # Fallback to legacy behavior (infer subnet from default route).
                self._subnetwork = None
                return await self.auto_detect(None)

            if add_type == VIRTUAL_LED:
                title = f"{VIRTUAL_LED}-{int(time())}"
                user_input[CONFIG_FLOW_IP_ADDRESS] = title
                user_input[CONFIG_FLOW_HW_MODEL] = VIRTUAL_LED
                user_input[CONFIG_FLOW_SCAN_INTERVAL] = VIRTUAL_LED_SCAN_INTERVAL
                _LOGGER.debug("Creating virtual LED entry with unique_id '%s'", title)
                await self.async_set_unique_id(title)
                return self.async_create_entry(title=title, data=user_input)

        # Step 3: create entry from submitted values
        _LOGGER.debug("Config flow submission keys: %s", list(user_input.keys()))

        # CLOUD
        if CONFIG_FLOW_CLOUD_USERNAME in user_input:
            valid = await validate_cloud_input(
                self.hass,
                str(user_input[CONFIG_FLOW_CLOUD_USERNAME]),
                str(user_input[CONFIG_FLOW_CLOUD_PASSWORD]),
            )
            if not valid:
                errors = {"base": "Authentification fail, check your crendentials"}
                schema = vol.Schema(
                    {
                        vol.Required(
                            CONFIG_FLOW_CLOUD_USERNAME,
                            default=user_input[CONFIG_FLOW_CLOUD_USERNAME],
                        ): str,
                        vol.Required(
                            CONFIG_FLOW_CLOUD_PASSWORD,
                            default=user_input[CONFIG_FLOW_CLOUD_PASSWORD],
                        ): str,
                    }
                )
                return self.async_show_form(
                    step_id="user", data_schema=schema, errors=errors
                )

            user_input[CONFIG_FLOW_SCAN_INTERVAL] = get_scan_interval(CLOUD_DEVICE_TYPE)
            user_input[CONFIG_FLOW_CONFIG_TYPE] = False
            user_input[CONFIG_FLOW_IP_ADDRESS] = CLOUD_SERVER_ADDR
            user_input[CONFIG_FLOW_HW_MODEL] = CLOUD_DEVICE_TYPE

            title = str(user_input[CONFIG_FLOW_CLOUD_USERNAME])
            await self.async_set_unique_id(title)
            return self.async_create_entry(title=title, data=user_input)

        # DETECT and MANUAL
        if CONFIG_FLOW_IP_ADDRESS in user_input:
            ip_value = str(user_input[CONFIG_FLOW_IP_ADDRESS])

            # Allow "Virtual LED" via manual field as before
            if ip_value == VIRTUAL_LED:
                title = f"{VIRTUAL_LED}-{int(time())}"
                user_input[CONFIG_FLOW_IP_ADDRESS] = title
                user_input[CONFIG_FLOW_HW_MODEL] = VIRTUAL_LED
                user_input[CONFIG_FLOW_SCAN_INTERVAL] = VIRTUAL_LED_SCAN_INTERVAL
                _LOGGER.debug("Creating virtual LED entry with unique_id '%s'", title)
                await self.async_set_unique_id(title)
                return self.async_create_entry(title=title, data=user_input)

            # If user provided a CIDR, run auto-detect
            if _is_cidr(ip_value):
                subnetwork = ip_value
                return await self.auto_detect(subnetwork)

            configuration = ip_value.split(" ")

            # Manual device: only IP provided -> attempt identify
            if len(configuration) < 2:
                ip = configuration[0]
                (
                    status,
                    ip,
                    hw_model,
                    friendly_name,
                    uuid,
                ) = await self.hass.async_add_executor_job(partial(is_reefbeat, ip=ip))
                _LOGGER.info(
                    "Manual probe: ip=%s hw=%s name=%s uuid=%s",
                    ip,
                    hw_model,
                    friendly_name,
                    uuid,
                )

                if status is True:
                    conf = _device_to_string(
                        {
                            "ip": ip,
                            "hw_model": hw_model or "",
                            "friendly_name": friendly_name or "",
                        }
                    )
                    configuration = conf.split(" ")
                else:
                    # Keep existing behavior: proceed, but unique_id will fall back to ip (below)
                    pass

            # Detected device string: resolve unique_id via description.xml
            uuid = await self._unique_id(user_input)
            _LOGGER.info("Resolved unique_id: %s", uuid)

            await self.async_set_unique_id(str(uuid))
            self._abort_if_unique_id_configured()

            title = (
                "-".join(configuration[2:])
                if len(configuration) >= 3
                else configuration[0]
            )
            user_input[CONFIG_FLOW_HW_MODEL] = (
                configuration[1] if len(configuration) >= 2 else ""
            )
            user_input[CONFIG_FLOW_IP_ADDRESS] = configuration[0]
            user_input[CONFIG_FLOW_SCAN_INTERVAL] = get_scan_interval(
                user_input[CONFIG_FLOW_HW_MODEL]
            )
            user_input[CONFIG_FLOW_CONFIG_TYPE] = False

            _LOGGER.info(
                "Creating entry: title=%s ip=%s hw=%s",
                title,
                user_input[CONFIG_FLOW_IP_ADDRESS],
                user_input[CONFIG_FLOW_HW_MODEL],
            )
            return self.async_create_entry(title=title, data=user_input)

        # Should not happen, but keep flow stable
        return self.async_abort(reason="unknown")

    async def async_step_select_subnetwork(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Prompt for subnet selection when multiple local subnets exist."""
        subnet_choices = await _async_get_ipv4_subnet_choices(self.hass)

        # If we no longer have multiple choices, just proceed.
        if len(subnet_choices) <= 1:
            return await self.auto_detect(subnet_choices[0] if subnet_choices else None)

        if user_input is None:
            # Also allow manual scan/probe by entering an explicit IP/CIDR.
            choices = [*subnet_choices, ENTER_IP]
            return self.async_show_form(
                step_id="select_subnetwork",
                data_schema=vol.Schema(
                    {vol.Required(CONFIG_FLOW_SUBNETWORK): vol.In(choices)}
                ),
            )

        if str(user_input[CONFIG_FLOW_SUBNETWORK]) == ENTER_IP:
            return await self.async_step_enter_ip()

        self._subnetwork = str(user_input[CONFIG_FLOW_SUBNETWORK])
        return await self.auto_detect(self._subnetwork)

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle device selection after scanning the chosen subnet."""
        if user_input is None:
            subnetwork = getattr(self, "_subnetwork", None)
            return await self.auto_detect(cast(str | None, subnetwork))

        if str(user_input.get(CONFIG_FLOW_IP_ADDRESS, "")) == ENTER_IP:
            return await self.async_step_enter_ip()

        # Reuse the main submission handler to create the entry.
        return await self.async_step_user(user_input)

    async def async_step_enter_ip(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Prompt for a manual IP/CIDR to probe/scan."""
        if user_input is None:
            return self.async_show_form(
                step_id="enter_ip",
                data_schema=vol.Schema({vol.Required(CONFIG_FLOW_IP_ADDRESS): str}),
            )

        value = str(user_input[CONFIG_FLOW_IP_ADDRESS]).strip()
        if _is_cidr(value):
            # Scan the user-provided CIDR.
            self._subnetwork = value
            return await self.auto_detect(value)

        # Plain IP: probe first; if it isn't a ReefBeat device, fall back to
        # scanning its /24 (common VLAN/subnet setup).
        try:
            ip = str(ipaddress.ip_address(value))
        except Exception:
            return self.async_show_form(
                step_id="enter_ip",
                data_schema=vol.Schema({vol.Required(CONFIG_FLOW_IP_ADDRESS): str}),
                errors={"base": "invalid_ip"},
            )

        (
            status,
            ip,
            hw_model,
            friendly_name,
            _uuid,
        ) = await self.hass.async_add_executor_job(partial(is_reefbeat, ip=ip))

        if status is True and hw_model:
            conf = _device_to_string(
                {
                    "ip": ip,
                    "hw_model": hw_model,
                    "friendly_name": friendly_name or "",
                }
            )
            return await self.async_step_user({CONFIG_FLOW_IP_ADDRESS: conf})

        # Not a ReefBeat device (or probe failed) -> scan the /24 of the IP.
        cidr = str(ipaddress.ip_network(f"{ip}/24", strict=False))
        self._select_device_note = (
            f"No ReefBeat device was found at {ip}. Scanned {cidr} instead."
        )
        _LOGGER.info("%s", self._select_device_note)
        self._subnetwork = cidr
        return await self.auto_detect(cidr)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler(config_entry)

    async def auto_detect(
        self, subnetwork: str | None
    ) -> config_entries.ConfigFlowResult:
        """Auto-detect ReefBeat devices and present a selection list."""
        detected_devices: list[ReefBeatInfo] = await self.hass.async_add_executor_job(
            partial(get_reefbeats, subnetwork=subnetwork)
        )
        # No need for deepcopy; we only remove items from the "available" view.
        available_devices: list[ReefBeatInfo] = list(detected_devices)

        _LOGGER.info("Detected devices: %s", detected_devices)

        existing = {e.unique_id for e in self._async_current_entries() if e.unique_id}
        for device in detected_devices:
            if device.get("uuid") in existing:
                _LOGGER.info(
                    "%s skipped (already configured)", device.get("friendly_name")
                )
                if device in available_devices:
                    available_devices.remove(device)

        _LOGGER.info("Available devices: %s", available_devices)

        available_devices_s = list(map(_device_to_string, available_devices))
        available_devices_s.append(ENTER_IP)

        if not available_devices and subnetwork:
            # Keep it informative: nothing found on this CIDR, but keep the
            # Enter IP/CIDR option available.
            self._select_device_note = (
                self._select_device_note
                or f"No ReefBeat devices were found on {subnetwork}."
            )

        note = self._select_device_note
        self._select_device_note = None

        return self.async_show_form(
            step_id="select_device",
            description_placeholders={
                # Leave blank unless we have something helpful to say.
                "scan_reason": f"\n\n{note}" if note else "",
            },
            data_schema=vol.Schema(
                {vol.Required(CONFIG_FLOW_IP_ADDRESS): vol.In(available_devices_s)}
            ),
        )


# Options flow
class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle integration options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # ReefBeatCloudAPI options
            if CONFIG_FLOW_CLOUD_USERNAME in user_input:
                user_input[CONFIG_FLOW_IP_ADDRESS] = self._config_entry.data[
                    CONFIG_FLOW_IP_ADDRESS
                ]
                user_input[CONFIG_FLOW_HW_MODEL] = self._config_entry.data[
                    CONFIG_FLOW_HW_MODEL
                ]
                user_input[CONFIG_FLOW_CONFIG_TYPE] = False

                valid = await validate_cloud_input(
                    self.hass,
                    str(user_input[CONFIG_FLOW_CLOUD_USERNAME]),
                    str(user_input[CONFIG_FLOW_CLOUD_PASSWORD]),
                )
                if not valid:
                    errors = {"base": "Authentification fail, check your crendentials"}
                    schema = vol.Schema(
                        {
                            vol.Required(
                                CONFIG_FLOW_CLOUD_USERNAME,
                                default=user_input[CONFIG_FLOW_CLOUD_USERNAME],
                            ): str,
                            vol.Required(
                                CONFIG_FLOW_CLOUD_PASSWORD,
                                default=user_input[CONFIG_FLOW_CLOUD_PASSWORD],
                            ): str,
                            vol.Required(
                                CONFIG_FLOW_SCAN_INTERVAL,
                                default=user_input[CONFIG_FLOW_SCAN_INTERVAL],
                            ): int,
                            vol.Required(CONFIG_FLOW_CONFIG_TYPE, default=False): bool,
                        }
                    )
                    return self.async_show_form(
                        step_id="init", data_schema=schema, errors=errors
                    )

                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data=user_input,
                    options=self._config_entry.options,
                )
                res = self.async_create_entry(data=user_input)
                _LOGGER.debug("Scheduling reload for %s", res.get("handler"))
                self.hass.config_entries.async_schedule_reload(res["handler"])
                return res

            # Generic scan interval/config type options
            if CONFIG_FLOW_SCAN_INTERVAL in user_input:
                data: dict[str, Any] = {
                    CONFIG_FLOW_IP_ADDRESS: self._config_entry.data[
                        CONFIG_FLOW_IP_ADDRESS
                    ],
                    CONFIG_FLOW_HW_MODEL: self._config_entry.data[CONFIG_FLOW_HW_MODEL],
                    CONFIG_FLOW_SCAN_INTERVAL: user_input[CONFIG_FLOW_SCAN_INTERVAL],
                    CONFIG_FLOW_CONFIG_TYPE: user_input[CONFIG_FLOW_CONFIG_TYPE],
                }
                if CONFIG_FLOW_INTENSITY_COMPENSATION in user_input:
                    data[CONFIG_FLOW_INTENSITY_COMPENSATION] = user_input[
                        CONFIG_FLOW_INTENSITY_COMPENSATION
                    ]

                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=data, options=self._config_entry.options
                )
                return self.async_create_entry(data=data)

            # Virtual LED linking options
            leds: dict[str, bool] = {}
            for led_key, enabled in user_input.items():
                if enabled:
                    leds[led_key] = True

            data = {
                CONFIG_FLOW_IP_ADDRESS: self._config_entry.data[CONFIG_FLOW_IP_ADDRESS],
                CONFIG_FLOW_HW_MODEL: VIRTUAL_LED,
                CONFIG_FLOW_SCAN_INTERVAL: VIRTUAL_LED_SCAN_INTERVAL,
                LINKED_LED: leds,
            }
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=data, options=self._config_entry.options
            )
            return self.async_create_entry(data=data)

        errors: dict[str, str] = {}
        options_schema: vol.Schema | None = None

        if not self._config_entry.title.startswith(VIRTUAL_LED + "-"):
            hw_model: str | None = None
            res = []
            try:
                hw_model = cast(str, self._config_entry.data[CONFIG_FLOW_HW_MODEL])
                query = parse('$[?(@.name=="' + hw_model + '")]')
                res = query.find(LEDS_INTENSITY_COMPENSATION)
            except Exception:
                hw_model = None
                res = []

            if len(res) > 0:
                options_schema = vol.Schema(
                    {
                        vol.Required(
                            CONFIG_FLOW_SCAN_INTERVAL,
                            default=get_scan_interval_safe(hw_model),
                        ): int,
                        vol.Required(CONFIG_FLOW_CONFIG_TYPE, default=False): bool,
                        vol.Required(
                            CONFIG_FLOW_INTENSITY_COMPENSATION, default=False
                        ): bool,
                    }
                )
            elif hw_model == CLOUD_DEVICE_TYPE:
                options_schema = vol.Schema(
                    {
                        vol.Required(
                            CONFIG_FLOW_CLOUD_USERNAME,
                            default=self._config_entry.data[CONFIG_FLOW_CLOUD_USERNAME],
                        ): str,
                        vol.Required(
                            CONFIG_FLOW_CLOUD_PASSWORD,
                            default=self._config_entry.data[CONFIG_FLOW_CLOUD_PASSWORD],
                        ): str,
                        vol.Required(
                            CONFIG_FLOW_SCAN_INTERVAL,
                            default=get_scan_interval_safe(hw_model),
                        ): int,
                        vol.Required(CONFIG_FLOW_CONFIG_TYPE, default=False): bool,
                    }
                )
            else:
                options_schema = vol.Schema(
                    {
                        vol.Required(
                            CONFIG_FLOW_SCAN_INTERVAL,
                            default=get_scan_interval_safe(hw_model),
                        ): int,
                        vol.Required(CONFIG_FLOW_CONFIG_TYPE, default=False): bool,
                    }
                )
        else:
            leds_schema: dict[Any, Any] = {}
            for dev_id in self.hass.data.get(DOMAIN, {}):
                led = self.hass.data[DOMAIN][dev_id]
                if type(led).__name__ in ("ReefLedCoordinator", "ReefLedG2Coordinator"):
                    key = f"LED-{led.model}-: {led.serial} ({dev_id})"
                    leds_schema[vol.Required(key)] = bool
            options_schema = vol.Schema(leds_schema)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                options_schema, self._config_entry.options
            ),
            errors=errors,
        )
