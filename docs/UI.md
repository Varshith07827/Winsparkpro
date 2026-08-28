# The window

```
┌───────────────────────────────┬──────────────────────────────────────┐
│ search                        │ V                                    │
│───────────────────────────────├──────────────────────────────────────┤
│ ☑ V            pong        2  │ 111111111111111@lid                  │
│ ☐ Team chat    Are you…       │ automation on                        │
│ ☑ Bob          Send the file  │──────────────────────────────────────│
│                               │  ← ping                       9:21   │
│                               │  → pong                       9:21   │
├───────────────────────────────┴──────────────────────────────────────┤
│ session ready · 919876500000 · 12 delivered · 4 replied · MongoDB ·  │
└──────────────────────────────────────────────────────────────────────┘
```

**Two things can be changed here** — a chat's tick box and its webhook URL.
Everything else is a read of what happened.

---

## The chat list

Every chat OpenWA can see appears here. The list is synced from OpenWA at
startup and every five minutes, and repaints the moment a message arrives, so
there is no "add chat" step and no refresh button.

Rows are named the way you would type them: the address-book name first, then
WhatsApp's own chat name, then the number, then the raw id. That order matters
on real data — a chat whose WhatsApp name is stylised unicode renders fine and
cannot be typed, so the contact name has to win.

A new chat arrives with its box **unticked**. Tick it and the chat starts being
answered; untick it and it stops. That is immediate and silent in both
directions.

Untiicking does **not** delete anything. The earlier version deleted the chat's
entire stored history when you unticked it, which is why it asked for
confirmation first — a stray click on a 14-pixel target would destroy a history
nothing could restore. Turning automation off should stop replies, not destroy
the history you turned it off in order to read, so both the deletion and the
dialog guarding it are gone.

Search matches the name, the number and the last message. The number is
included deliberately: for a chat named in unicode art, it is the only thing a
person can actually type.

---

## The detail panel

Click a chat to see:

- **Its name**, from the address book where there is one.
- **Its identity** — the chat id, and a phone number when there genuinely is
  one. A `@lid` chat has no derivable number, so only the id is shown. This is
  deliberate: a plausible-looking number that belongs to nobody is worse than
  no number.
- **Its state** — automation on or off, whether it is a group, and any last
  error.
- **The transcript** — the last 200 messages, `←` in and `→` out. A failed send
  is the one thing coloured.

- **Its webhook** — where this chat's incoming messages are POSTed. Empty means
  the global `DEFAULT_WEBHOOK`; with neither, nothing is dispatched. Saved as
  you type, with no button: a save button on a single field is a button whose
  only purpose is to be forgotten. Beneath it, what the endpoint last said —
  **including failures**, because "answered 502 after 3 retries" is exactly
  what someone debugging a silent chat needs.

One field that used to be here is gone: **the contact's phone number**. It
existed because WhatsApp Desktop would not give a number up — it shows a saved
contact by name and exposes the number nowhere readable (measured: zero
phone-shaped strings across every accessible name in the window) — and the
webhook URL was built from it. OpenWA resolves the number itself now.

---

## The status bar

| Reads | Means |
|---|---|
| `session ready · 919876500000` | OpenWA's session state and linked number. Red if anything but `ready`. |
| `908 contacts · 12 delivered · 4 replied` | The address book, and what the listener has done. `· 2 hook failed` appears only when non-zero. |
| `not listening` | The webhook port could not be bound |
| `MongoDB connected · wa_events` | |
| `JSON ok · 09:21` | Last mirror write |

The delivery count is the most useful number on the screen. When a chat is
ticked and nothing happens, `0 delivered` says *OpenWA is not reaching this
process* — a webhook pointing at `localhost` instead of `host.docker.internal`,
or its SSRF guard blocking the address. A green session light says the
opposite and would be misleading on its own.

---

## Startup

Load → validate → launch. A configuration problem shows a startup screen
listing **every** problem at once, with a Retry button, so you can fix `.env` in
another window and try again without restarting.

First run asks for four things — MongoDB URI, and OpenWA's address, API key and
session id — tests both connections before writing `.env`, and never asks
again. It is not a settings screen: there is no way back into it from the
running application.

Non-fatal problems appear as a warning dialog rather than blocking the launch.

---

## Theme

Light and dark, following the operating system, and it re-styles live if you
change the system setting while the window is open.

---

## Running without it

`python run_headless.py` starts the service with no window — for a server, a
container, or a check that the pipeline works with no display attached.
Everything works except the tick box, so chats have to be switched on from the
window once, or in the database.
