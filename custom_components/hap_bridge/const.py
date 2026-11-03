"""Constants for HAP Bridge."""

DOMAIN = "hap_bridge"

CONF_INCLUDE_INTEGRATIONS = "include_integrations"

DEFAULT_NAME = "HAP Bridge"
DEFAULT_PORT = 21070

# The bridge itself is always AID 1; a HAP bridge may hold 149 more.
BRIDGE_AID = 1
MAX_BRIDGED_ACCESSORIES = 149

# Name of the platform module an integration ships to describe its devices,
# e.g. custom_components/acme/hap_bridge.py
PLATFORM = DOMAIN

STORAGE_VERSION = 1
