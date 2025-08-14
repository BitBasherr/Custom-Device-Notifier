DOMAIN = "custom_device_notifier"

CONF_SERVICE_NAME = "service_name"
CONF_SERVICE_NAME_RAW = "service_name_raw"
CONF_TARGETS = "targets"
CONF_PRIORITY = "priority"
CONF_FALLBACK = "fallback"
CONF_MATCH_MODE = "match_mode"

KEY_SERVICE = "service"
KEY_CONDITIONS = "conditions"
KEY_MATCH = "match"

STEP_USER = "user"
STEP_ADD_TARGET = "add_target"
STEP_ADD_COND_ENTITY = "add_condition_entity"
STEP_ADD_COND_VALUE = "add_condition_value"
STEP_COND_MORE = "condition_more"
STEP_REMOVE_COND = "remove_condition"
STEP_MATCH_MODE = "match_mode"
STEP_TARGET_MORE = "target_more"
STEP_ORDER_TARGETS = "order_targets"
STEP_CHOOSE_FALLBACK = "choose_fallback"

# ── Routing mode (new) ────────────────────────────────────────────────
CONF_ROUTING_MODE = "routing_mode"        # "conditional" or "smart"
ROUTING_CONDITIONAL = "conditional"
ROUTING_SMART = "smart"
DEFAULT_ROUTING_MODE = ROUTING_CONDITIONAL

# ── Smart Select config (new) ─────────────────────────────────────────
CONF_SMART_PC_NOTIFY = "smart_pc_notify"                  # e.g. notify.desktop_pop
CONF_SMART_PC_SESSION = "smart_pc_session_sensor"         # e.g. sensor.desktop_session_state
CONF_SMART_PHONE_ORDER = "smart_phone_order"              # list[str] of notify.mobile_app_*

CONF_SMART_MIN_BATTERY = "smart_min_battery"              # int
CONF_SMART_PHONE_FRESH_S = "smart_phone_fresh_s"          # int
CONF_SMART_PC_FRESH_S = "smart_pc_fresh_s"                # int
CONF_SMART_REQUIRE_AWAKE = "smart_require_awake"          # bool
CONF_SMART_REQUIRE_UNLOCKED = "smart_require_unlocked"    # bool
CONF_SMART_POLICY = "smart_policy"                        # one of SMART_POLICY_*

# Policies
SMART_POLICY_PC_FIRST = "pc_first"
SMART_POLICY_PHONE_IF_PC_UNLOCKED = "phone_if_pc_unlocked"
SMART_POLICY_PHONE_FIRST = "phone_first"

# Reasonable defaults
DEFAULT_SMART_MIN_BATTERY = 2
DEFAULT_SMART_PHONE_FRESH_S = 180
DEFAULT_SMART_PC_FRESH_S = 300
DEFAULT_SMART_REQUIRE_AWAKE = True
DEFAULT_SMART_REQUIRE_UNLOCKED = True
DEFAULT_SMART_POLICY = SMART_POLICY_PC_FIRST
