"""Set a chat's Contact ID — the identifier the send API addresses it by.

    python set-contact-id.py "Varshith" 9423

Run it with the application closed. A running instance holds its own copy of
every chat in memory and will write that copy back over this one on its next
poll — the same reason the configuration panel is the usual place to do this.
"""

import sys

from wadam.config import load_settings
from wadam.domain.models import chat_id_for
from wadam.storage.mongo import MongoStore

if len(sys.argv) != 3:
    raise SystemExit(__doc__)

chat_name, contact_id = sys.argv[1], sys.argv[2]
settings = load_settings()
mongo = MongoStore(settings.mongodb_uri, settings.database_name)
mongo.connect()
result = mongo.chat_configs.update_one(
    {"chat_id": chat_id_for(chat_name)}, {"$set": {"external_id": contact_id}}
)
if result.matched_count:
    print(f"{chat_name!r} contact ID -> {contact_id!r}")
else:
    known = sorted(d.get("chat_name", "") for d in mongo.chat_configs.find({}, {"chat_name": 1}))
    print(f"No chat named {chat_name!r}. Known chats: {known}")
mongo.close()
