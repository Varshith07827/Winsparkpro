# Limitations

Honest boundaries. Most of the old limitations were consequences of reading a
desktop window; those are gone. These are what remain.

---

## Text only

The webhook's reply is sent as text, and `OpenWAClient` sends text. Inbound media
arrives with a `media_kind` (`image`, `audio`, `document`, …) and an empty
`text`, so a rule can *notice* a photo but not read it or answer with one.

OpenWA exposes `send-image`, `send-video`, `send-audio`, `send-document`,
`send-sticker`, `send-location` and `send-poll`. Adding one is a method on
`OpenWAClient` plus a branch in the pipeline — a small change, just not one
that has been made.

Worth noting what *did* improve: the old transport could never retrieve media
at all. The accessibility tree can name an attachment but never hand over its
bytes, so a voice note arrived as the literal string `[Voice note · 0:12]`.
Media is now genuinely reachable; it simply is not wired up.

---

## One session

`OPENWA_SESSION_ID` is a single value. Running two WhatsApp numbers means two
copies of this application, with different `WEBHOOK_PORT`s and different
databases.

---

## A ten-digit number is refused

Callers must include the country code. India and the US both use ten national
digits, so `9100251854` is a real person in either country and expanding it
would send to whichever the guess landed on. The refusal says so.

---

## Replies are stateless

The payload carries one message and its chat. Your endpoint gets no
conversation history, so anything multi-turn has to keep its own state — which
is the right place for it, since only the endpoint knows what state means.

---

## The cooldown is per chat, not per rule

One quiet period per chat, applied to every reply. Two rules that should have
different pacing cannot. `COOLDOWN_SECONDS=0` disables it, which is only
sensible when the other end is definitely a person.

---

## No delivery confirmation

A `200` from OpenWA means the gateway accepted the message. It does not mean
WhatsApp delivered it, and it does not mean it was read. OpenWA tracks
`message.ack` events, but this application does not subscribe to them — it
subscribes to `message.received` only.

The old transport verified harder than this, by counting outgoing bubbles in
the conversation. That was worth doing when a send could silently half-happen.
Against an API it would be re-verifying something the response already
answered.

---

## A failed send is not retried

Deliberate. A retry would call your endpoint again and could deliver twice, and a
duplicate is worse than a miss. The message is stored as `failed` with the
reason, and surfaced in the transcript for a person to deal with.

See `OPERATIONS.md` for the case where OpenWA reported a failure for a message
it had already delivered — precisely the scenario that makes retrying wrong.

---

## The window is single-user and local

No authentication, no multi-user access, no remote view. It reads and writes a
MongoDB this machine can reach. Two copies pointed at the same database would
both answer the same messages.

---

## Scale

Fine for personal and small-team use — a handful of chats, a message every few
seconds. Each delivery is a thread; a burst of hundreds would be handled but
`ThreadingHTTPServer` is not the tool for sustained high volume, and WhatsApp
would rate-limit long before this became the bottleneck.

---

## No test coverage of the GUI

The pipeline, transport, storage, config, directory, webhook client and send
API are covered — 165 tests.
The PySide6 widgets are not. The previous version's widget tests went with the
widgets they tested, and their replacements have not been written. Launching
the window is currently a manual check.

---

## The standing one

OpenWA is an **unofficial** WhatsApp client. It connects through
reverse-engineered clients, not Meta's Cloud API, and there is always a
non-zero risk of the account being restricted. No amount of care in this
codebase changes that.

Use a dedicated number you can afford to lose. Rate-limit yourself. Reply to
people who expect to hear from you rather than cold-messaging strangers — that
last one is the single most reliable way to get a number restricted.

For anything where compliance matters — healthcare, finance, large-scale
commercial messaging, end users in the EU/EEA — this is not the right tool. Use
[Meta's official Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api).
