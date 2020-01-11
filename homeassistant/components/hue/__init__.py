"""Support for the Philips Hue system."""
import asyncio
import ipaddress
import logging

from aiohue.util import normalize_bridge_id
import voluptuous as vol

from homeassistant import config_entries, core
from homeassistant.const import CONF_HOST
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .bridge import (
    ATTR_GROUP_NAME,
    ATTR_SCENE_NAME,
    SCENE_SCHEMA,
    SERVICE_HUE_SCENE,
    HueBridge,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_BRIDGES = "bridges"

CONF_ALLOW_UNREACHABLE = "allow_unreachable"
DEFAULT_ALLOW_UNREACHABLE = False

DATA_CONFIGS = "hue_configs"

PHUE_CONFIG_FILE = "phue.conf"

CONF_ALLOW_HUE_GROUPS = "allow_hue_groups"
DEFAULT_ALLOW_HUE_GROUPS = True

BRIDGE_CONFIG_SCHEMA = vol.Schema(
    {
        # Validate as IP address and then convert back to a string.
        vol.Required(CONF_HOST): vol.All(ipaddress.ip_address, cv.string),
        vol.Optional(
            CONF_ALLOW_UNREACHABLE, default=DEFAULT_ALLOW_UNREACHABLE
        ): cv.boolean,
        vol.Optional(
            CONF_ALLOW_HUE_GROUPS, default=DEFAULT_ALLOW_HUE_GROUPS
        ): cv.boolean,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_BRIDGES): vol.All(
                    cv.ensure_list, [BRIDGE_CONFIG_SCHEMA]
                )
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, config):
    """Set up the Hue platform."""

    async def hue_activate_scene(call, skip_reload=True):
        """Handle activation of Hue scene."""
        # Get parameters
        group_name = call.data[ATTR_GROUP_NAME]
        scene_name = call.data[ATTR_SCENE_NAME]

        # Call the set scene function on each bridge
        tasks = [
            bridge.hue_activate_scene(
                call, updated=skip_reload, hide_warnings=skip_reload
            )
            for bridge in hass.data[DOMAIN].values()
            if isinstance(bridge, HueBridge)
        ]
        results = await asyncio.gather(*tasks)

        # Did *any* bridge succeed? If not, refresh / retry
        # Note that we'll get a "None" value for a successful call
        if None not in results:
            if skip_reload:
                return await hue_activate_scene(call, skip_reload=False)
            _LOGGER.warning(
                "No bridge was able to activate " "scene %s in group %s",
                scene_name,
                group_name,
            )

    # Set up the Hue platform.
    conf = config.get(DOMAIN)
    if conf is None:
        conf = {}

    hass.data[DOMAIN] = {}
    hass.data[DATA_CONFIGS] = {}

    # Register a local handler for scene activation
    hass.services.async_register(
        DOMAIN, SERVICE_HUE_SCENE, hue_activate_scene, schema=SCENE_SCHEMA
    )

    # User has configured bridges
    if CONF_BRIDGES not in conf:
        return True

    bridges = conf[CONF_BRIDGES]

    configured_hosts = set(
        entry.data["host"] for entry in hass.config_entries.async_entries(DOMAIN)
    )

    for bridge_conf in bridges:
        host = bridge_conf[CONF_HOST]

        # Store config in hass.data so the config entry can find it
        hass.data[DATA_CONFIGS][host] = bridge_conf

        if host in configured_hosts:
            continue

        # No existing config entry found, trigger link config flow. Because we're
        # inside the setup of this component we'll have to use hass.async_add_job
        # to avoid a deadlock: creating a config entry will set up the component
        # but the setup would block till the entry is created!
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data={"host": bridge_conf[CONF_HOST]},
            )
        )

    return True


async def async_setup_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
):
    """Set up a bridge from a config entry."""
    host = entry.data["host"]
    config = hass.data[DATA_CONFIGS].get(host)

    if config is None:
        allow_unreachable = DEFAULT_ALLOW_UNREACHABLE
        allow_groups = DEFAULT_ALLOW_HUE_GROUPS
    else:
        allow_unreachable = config[CONF_ALLOW_UNREACHABLE]
        allow_groups = config[CONF_ALLOW_HUE_GROUPS]

    bridge = HueBridge(hass, entry, allow_unreachable, allow_groups)

    if not await bridge.async_setup():
        return False

    hass.data[DOMAIN][host] = bridge
    config = bridge.api.config

    # For backwards compat
    if entry.unique_id is None:
        hass.config_entries.async_update_entry(
            entry, unique_id=normalize_bridge_id(config.bridgeid)
        )

    device_registry = await dr.async_get_registry(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, config.mac)},
        identifiers={(DOMAIN, config.bridgeid)},
        manufacturer="Signify",
        name=config.name,
        model=config.modelid,
        sw_version=config.swversion,
    )

    if config.swupdate2_bridge_state == "readytoinstall":
        err = "Please check for software updates of the bridge in the Philips Hue App."
        _LOGGER.warning(err)

    return True


async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    bridge = hass.data[DOMAIN].pop(entry.data["host"])
    hass.services.async_remove(DOMAIN, SERVICE_HUE_SCENE)
    return await bridge.async_reset()
