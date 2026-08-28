"""Fixed values. Anything in here is deliberately NOT configurable."""

from __future__ import annotations

APP_NAME = "WhatsApp Desktop Automation Manager"
APP_SHORT_NAME = "WADAM"
APP_VERSION = "1.0.0"

# The poll cadence, hardcoded per the design: one chat-list read every three
# seconds, forever. There is no setting, no UI control, and no .env key that
# changes it. Deep per-chat work (opening a chat, running a webhook, sending a
# reply) happens on a separate worker so a slow send never stretches this.

# Most messages one relay tick takes from a chat's webhook before giving the
# loop its turn back. A webhook hands over one message per request, so without
# this a burst arrives one message per poll interval. Bounded so an endpoint
# that returns something new on every request cannot hold the tick open.
MAX_RELAY_DRAIN = 10

# --- what MongoDB is asked to do, and how often ----------------------------
# Atlas bills per operation, so anything on a timer is a standing charge that
# accrues whether or not the application did any work. Measured on an idle
# five-chat install: 2 operations per 3-second cycle, 1.7 MILLION a month, for
# nothing happening. These two intervals are where that went.

#: Seconds between writing "last seen this chat" to MongoDB. Kept in memory and
#: in the JSON mirror continuously; it is telemetry nobody decides anything
#: from, so paying per-cycle for it is the wrong trade.
POLL_TOUCH_INTERVAL = 300.0

#: Seconds between re-reading chat CONFIGURATION from MongoDB, so an edit made
#: outside this process takes effect without a restart.
#:
#: This ran every cycle for a while, and had to: every save wrote the whole
#: chat, so a routine write stamped stale config back over an external edit and
#: only a reload faster than the writes could win.  fixed
#: that properly — a save no longer writes back a config field it did not
#: change — so the reload no longer has a race to win and can cost what it
#: should.
CHAT_CONFIG_RELOAD_INTERVAL = 30.0


# How many message bubbles are read out of a conversation per pass. WhatsApp
# only realizes the visible tail into the accessibility tree anyway, so a
# larger number costs nothing but doesn't buy history either.
MESSAGE_READ_LIMIT = 25

# MongoDB collections.
COLLECTION_CHAT_CONFIGS = "chat_configs"
COLLECTION_MESSAGES = "messages"
COLLECTION_CONTACTS = "contacts"
COLLECTION_APPLICATION_STATE = "application_state"

#: Retired from MongoDB and kept locally instead — named here only so an
#: existing database can be told to drop them.
#:
#: `automation_logs` was a billable write per log line, and stored three times
#: over: the ring buffer feeds logs.json, and wadam.log gets the same line.
#: `poll_state` was a write every ten cycles to remember how many times a loop
#: had run — meaningless after a restart.
RETIRED_COLLECTIONS = ("automation_logs", "poll_state", "webhooks", "outgoing_queue")

# Both singleton collections hold exactly one document under this _id.
SINGLETON_ID = "singleton"

# JSON mirror filenames, written into JSON_BACKUP_FOLDER.
JSON_CHATS = "chats.json"
JSON_MESSAGES = "messages.json"
JSON_CONTACTS = "contacts.json"
JSON_AUTOMATION = "automation.json"
JSON_APP_STATE = "app_state.json"
JSON_LOGS = "logs.json"
JSON_SETTINGS = "settings.json"

# The JSON mirror is a backup, not an archive: unbounded history would turn
# every flush into a multi-megabyte rewrite. MongoDB keeps everything; these
# caps only trim what the mirror carries (newest first).
JSON_MESSAGE_LIMIT = 5000
JSON_LOG_LIMIT = 2000


#: The default MongoDB database. Overridable with DATABASE_NAME, because one
#: cluster often serves more than one deployment and a staging run should not
#: share a database with real messages.
DATABASE_NAME = "wa_events"

# There were four webhook-template constants here — a `{phone_number}`
# placeholder, a `{chat_name}` one, an empty default and an example of the
# shape — because every chat got its own URL derived from a global template.
# There is one webhook now, registered against the session inside OpenWA, so
# there is no template and nothing to substitute into it.
