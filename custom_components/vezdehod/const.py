"""Constants for the private Vezdehod integration."""

from datetime import timedelta

DOMAIN = "vezdehod"
CONF_AUTH_PHONE = "auth_phone"
CONF_MOBILE_KEY = "mobile_key"
CONF_BARRIER_IDS = "barrier_ids"
CONF_SAVE_SNAPSHOTS = "save_snapshots"
UPDATE_INTERVAL = timedelta(minutes=30)
PRESS_COOLDOWN = 3.0
SNAPSHOT_DELAY = 3.0
