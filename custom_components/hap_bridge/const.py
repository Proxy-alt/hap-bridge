"""Constants for HAP Bridge."""

DOMAIN = "hap_bridge"

CONF_INCLUDE_INTEGRATIONS = "include_integrations"
CONF_INCLUDE_DOMAINS = "include_domains"
CONF_EXCLUDE_DEVICES = "exclude_devices"
CONF_EXCLUDE_ENTITIES = "exclude_entities"
# {accessory key: name} chosen in the options flow.
CONF_NAMES = "names"
# Camera entities whose stream's audio goes to HomeKit too.
CONF_CAMERA_AUDIO = "camera_audio"
# Camera entities that record (HomeKit Secure Video) without a prebuffer.
CONF_NO_PREBUFFER = "camera_no_prebuffer"

DEFAULT_NAME = "HAP Bridge"
DEFAULT_PORT = 21070

# The bridge itself is always AID 1; a HAP bridge may hold 149 more.
BRIDGE_AID = 1
MAX_BRIDGED_ACCESSORIES = 149

# Name of the platform module an integration ships to describe its devices,
# e.g. custom_components/acme/hap_bridge.py
PLATFORM = DOMAIN

STORAGE_VERSION = 1

# Seconds to wait after a device/entity registry change before rebuilding the
# bridge, so a newly added device's burst of entities causes one reload.
REGISTRY_RELOAD_COOLDOWN = 30
