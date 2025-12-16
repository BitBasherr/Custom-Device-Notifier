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

# ── Routing mode (core) ────────────────────────────────────────────────
CONF_ROUTING_MODE = "routing_mode"  # "conditional" or "smart"
ROUTING_CONDITIONAL = "conditional"
ROUTING_SMART = "smart"
DEFAULT_ROUTING_MODE = ROUTING_CONDITIONAL

# ── Smart Select config ────────────────────────────────────────────────
CONF_SMART_PC_NOTIFY = "smart_pc_notify"  # e.g. notify.desktop_pop
CONF_SMART_PC_SESSION = "smart_pc_session_sensor"  # e.g. sensor.desktop_session_state
CONF_SMART_PHONE_ORDER = "smart_phone_order"  # list[str] of notify.mobile_app_*

CONF_SMART_MIN_BATTERY = "smart_min_battery"  # int
CONF_SMART_PHONE_FRESH_S = "smart_phone_fresh_s"  # int
CONF_SMART_PC_FRESH_S = "smart_pc_fresh_s"  # int
CONF_SMART_REQUIRE_AWAKE = "smart_require_awake"  # bool
CONF_SMART_REQUIRE_UNLOCKED = "smart_require_unlocked"  # bool
CONF_SMART_POLICY = "smart_policy"  # one of SMART_POLICY_*

# --- Smart Select extras (phone unlock stickiness) ---
CONF_SMART_PHONE_UNLOCK_WINDOW_S = "smart_phone_unlock_window_s"
DEFAULT_SMART_PHONE_UNLOCK_WINDOW_S = 120  # seconds

# require phones to be unlocked/awake to take priority
CONF_SMART_REQUIRE_PHONE_UNLOCKED = "smart_require_phone_unlocked"
DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED = False

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

# ── Audio / TTS (new, optional) ────────────────────────────────────────
CONF_TTS_ENABLE = "tts_enable"  # show Audio/TTS controls in UI; keep settings
CONF_TTS_DEFAULT = "tts_default"  # if true, speak by default unless overridden
CONF_TTS_SERVICE = (
    "tts_service"  # e.g. "tts.speak" (default) or legacy "tts.google_translate_say"
)
CONF_TTS_LANGUAGE = "tts_language"  # optional language string
CONF_MEDIA_PLAYER_ORDER = (
    "media_player_order"  # list[str] media_player.* in preference order
)

DEFAULT_TTS_ENABLE = False
DEFAULT_TTS_DEFAULT = False
DEFAULT_TTS_SERVICE = "tts.speak"
DEFAULT_TTS_LANGUAGE = ""

# ──────────────────────────── step IDs ────────────────────────────────
STEP_USER = "user"
STEP_ROUTING_MODE = "routing_mode"  # ask this right after name
STEP_ADD_TARGET = "add_target"
STEP_ADD_COND_ENTITY = "add_condition_entity"
STEP_ADD_COND_VALUE = "add_condition_value"
STEP_COND_MORE = "condition_more"
STEP_REMOVE_COND = "remove_condition"
STEP_SELECT_COND_TO_EDIT = "select_condition_to_edit"
STEP_MATCH_MODE = "match_mode"
STEP_TARGET_MORE = "target_more"
STEP_ORDER_TARGETS = "order_targets"
STEP_CHOOSE_FALLBACK = "choose_fallback"
STEP_SELECT_TARGET_TO_EDIT = "select_target_to_edit"
STEP_SELECT_TARGET_TO_REMOVE = "select_target_to_remove"
STEP_SMART_SETUP = "smart_setup"  # smart branch
STEP_SMART_ORDER_PHONES = "smart_phone_order"  # smart branch

# Audio/TTS branch
STEP_AUDIO_SETUP = "audio_setup"
STEP_MEDIA_ORDER = "media_order"

# Audio/TTS options (stored by the options flow)
TTS_OPT_ENABLE = "tts_enable"
TTS_OPT_DEFAULT = "tts_default"
TTS_OPT_SERVICE = "tts_service"  # e.g. "tts.google_translate_say" or "tts.speak"
TTS_OPT_LANGUAGE = "tts_language"  # optional language code
MEDIA_ORDER_OPT = "media_order"  # list[str] of media_player entity_ids

# Sticky preferred target right after boot (e.g., 2 minutes)
CONF_BOOT_STICKY_TARGET_S = "boot_sticky_target_s"
DEFAULT_BOOT_STICKY_TARGET_S = 120
_BOOT_STICKY_TARGET_S = DEFAULT_BOOT_STICKY_TARGET_S  # fallback if no option set

# ── Messages Bridge (mirror & reply) ─────────────────────────────────────
CONF_MSG_ENABLE = "msg_enable"
CONF_MSG_SOURCE_SENSOR = "msg_source_sensor"  # sensor.<slug>_last_notification
CONF_MSG_APPS = "msg_apps"  # list[str] of Android package names
CONF_MSG_TARGETS = "msg_targets"  # list[str] of notify.* to forward to
CONF_MSG_REPLY_TRANSPORT = "msg_reply_transport"  # "kdeconnect" | "tasker"
CONF_MSG_KDECONNECT_DEVICE_ID = "msg_kdeconnect_device_id"
CONF_MSG_TASKER_EVENT = "msg_tasker_event"  # HA event name for Tasker

DEFAULT_MSG_ENABLE = False
DEFAULT_MSG_APPS = ["com.google.android.apps.messaging"]  # Google Messages
DEFAULT_MSG_REPLY_TRANSPORT = "kdeconnect"
DEFAULT_MSG_TASKER_EVENT = "custom_device_notifier.reply"

# Config-flow step id
STEP_MESSAGES_SETUP = "messages_setup"

# ── Medication Tracking ─────────────────────────────────────────────────
CONF_MEDICATIONS = "medications"  # list of medication configs
CONF_MED_NAME = "med_name"
CONF_MED_SCHEDULE = "med_schedule"  # list of scheduled times per day
CONF_MED_ENABLED = "med_enabled"

# Storage
MEDICATION_STORAGE_VERSION = 1
MEDICATION_STORAGE_KEY = f"{DOMAIN}_medications"

# Services
SERVICE_MARK_TAKEN = "mark_medication_taken"
SERVICE_MARK_ALL_TAKEN = "mark_all_medications_taken"

# Panel
PANEL_NAME = "medication_tracker"
PANEL_TITLE = "Medications"
PANEL_ICON = "mdi:pill"
PANEL_URL = "/api/panel_custom/medication_tracker"
