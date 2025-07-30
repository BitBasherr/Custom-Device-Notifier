DOMAIN = "custom_device_notifier"

# config-entry data keys
CONF_SERVICE_NAME     = "service_name"         # snake_case slug
CONF_SERVICE_NAME_RAW = "service_name_raw"     # human name with spaces
CONF_TARGETS          = "targets"              # list of target dicts
CONF_PRIORITY         = "priority"             # ordered list of service IDs
CONF_FALLBACK         = "fallback"             # fallback service_id
CONF_MATCH_MODE       = "match_mode"           # per-target: "all" or "any"

# keys inside each target dict
KEY_SERVICE    = "service"                    # the notify service to call
KEY_CONDITIONS = "conds"                      # list of {entity, operator, value}
KEY_MATCH      = CONF_MATCH_MODE              # alias for match_mode
