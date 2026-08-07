"""Fixed values. Anything in here is deliberately NOT configurable."""

from __future__ import annotations

APP_NAME = "WhatsApp Desktop Automation Manager"
APP_SHORT_NAME = "WADAM"
APP_VERSION = "1.0.0"

# The poll cadence, hardcoded per the design: one chat-list read every three
# seconds, forever. There is no setting, no UI control, and no .env key that
# changes it. Deep per-chat work (opening a chat, running a webhook, sending a
# reply) happens on a separate worker so a slow send never stretches this.
POLL_INTERVAL_SECONDS = 3

# How many message bubbles are read out of a conversation per pass. WhatsApp
# only realizes the visible tail into the accessibility tree anyway, so a
# larger number costs nothing but doesn't buy history either.
MESSAGE_READ_LIMIT = 25

# MongoDB collections.
COLLECTION_CHAT_CONFIGS = "chat_configs"
COLLECTION_MESSAGES = "messages"
COLLECTION_WEBHOOKS = "webhooks"
COLLECTION_OUTGOING = "outgoing_queue"
COLLECTION_AUTOMATION_LOGS = "automation_logs"
COLLECTION_APPLICATION_STATE = "application_state"
COLLECTION_POLL_STATE = "poll_state"

# Both singleton collections hold exactly one document under this _id.
SINGLETON_ID = "singleton"

# JSON mirror filenames, written into JSON_BACKUP_FOLDER.
JSON_CHATS = "chats.json"
JSON_MESSAGES = "messages.json"
JSON_WEBHOOKS = "webhooks.json"
JSON_OUTGOING = "outgoing.json"
JSON_AUTOMATION = "automation.json"
JSON_APP_STATE = "app_state.json"
JSON_LOGS = "logs.json"
JSON_SETTINGS = "settings.json"

# The JSON mirror is a backup, not an archive: unbounded history would turn
# every flush into a multi-megabyte rewrite. MongoDB keeps everything; these
# caps only trim what the mirror carries (newest first).
JSON_MESSAGE_LIMIT = 5000
JSON_WEBHOOK_LIMIT = 2000
JSON_LOG_LIMIT = 2000

# Rows kept in the automation_logs collection before the oldest are pruned.
LOG_RETENTION_ROWS = 20000
