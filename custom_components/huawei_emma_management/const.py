from .connector_contract import (
    DEFAULT_API_PORT,
    DEFAULT_TLS_PORT,
    TOU_MAX_PERIODS,
)


DOMAIN = "huawei_emma_management"
PLATFORMS = [
    "sensor",
    "select",
    "switch",
    "number",
    "datetime",
    "time",
    "button",
    "text",
]
CONF_TOKEN = "token"
CONF_MODE = "mode"
CONF_TLS_PORT = "tls_port"
CONF_CERTIFICATE_MODE = "certificate_mode"
CONF_CERTIFICATE_NAME = "certificate_name"
CONF_CERTIFICATE_PATH = "certificate_path"
CONF_PRIVATE_KEY_PATH = "private_key_path"
CONF_PRIVATE_KEY_PASSWORD = "private_key_password"
CONF_DISCOVERED_DEVICE = "discovered_device"
MODE_EMBEDDED = "embedded"
MODE_EXTERNAL = "external"
CERTIFICATE_AUTOMATIC = "automatic"
CERTIFICATE_CUSTOM = "custom"
CONF_ACCEPT_EXTERNAL_GROWATT_CONTROLS = "accept_external_growatt_controls"
DEFAULT_ACCEPT_EXTERNAL_GROWATT_CONTROLS = True
DEFAULT_PORT = DEFAULT_API_PORT
TOU_REGISTER_NAME = "emma_tou_periods"
TOU_MODES = ("charge", "discharge")
GROWATT_COMPAT_DOMAIN = "growatt_server"
HUAWEI_EXTERNAL_API_DOMAIN = "huawei_emma"
GROWATT_MAX_SEGMENTS = 9
GROWATT_BATTERY_MODES = ("load_first", "battery_first", "grid_first")
