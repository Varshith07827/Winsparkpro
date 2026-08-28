"""A throwaway endpoint, to prove the loop end to end.

Not part of the application. It stands in for whatever you would really put
behind a webhook URL: it prints the payload wadam POSTed, answers with a reply,
and exercises the parts of the contract that are easy to get wrong.

    python test_endpoint.py            # answers everything
    python test_endpoint.py --silent   # answers empty, to prove silence works

Route the reply by what arrives:

    "ping"   -> "pong"
    "time"   -> the current time
    "quiet"  -> nothing, which is a success and must not be retried
    "boom"   -> HTTP 500, to watch wadam retry and then give up
    anything -> an echo
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9000


def show(text: str) -> None:
    """Print without ever raising.

    A chat name can be stylised unicode — WhatsApp allows it, and one of the
    chats this was tested against is named in mathematical bold script. Printing
    that to a cp1252 console raises UnicodeEncodeError *inside the request
    handler*, which kills the connection before a response is written and the
    caller sees "Remote end closed connection without response". Flushed too,
    because a redirected stdout is block-buffered and the log would otherwise
    look empty while requests were arriving.
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


SILENT = "--silent" in sys.argv
seen = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # the prints below are the log

    def do_POST(self):  # noqa: N802 - http.server naming
        try:
            self._handle_post()
        except Exception as error:  # noqa: BLE001
            show(f"      !! handler raised: {type(error).__name__}: {error}")
            try:
                self._reply(500, {"error": "handler raised"})
            except Exception:  # noqa: BLE001 - the socket may already be gone
                pass

    def _handle_post(self):
        global seen
        seen += 1
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")

        try:
            payload = json.loads(raw)
        except ValueError:
            show(f"\n[{seen}] unparseable body: {raw[:200]}")
            return self._reply(400, {"error": "not json"})

        chat = payload.get("chat", {})
        message = payload.get("message", {})
        text = (message.get("text") or "").strip()

        show(f"\n[{seen}] {payload.get('event')}")
        show(f"      chat    {chat.get('name')!r}  {chat.get('id')}  phone={chat.get('phone')}")
        show(f"      message {text!r}  key={message.get('key')}")
        show(f"      auth    {self.headers.get('Authorization') or '(none)'}")

        if SILENT:
            show("      -> answering empty (silent mode)")
            return self._reply(204, None)

        lowered = text.lower()
        if lowered == "boom":
            show("      -> 500, so wadam retries and then gives up")
            return self._reply(500, {"error": "deliberate"})
        if lowered == "quiet":
            show("      -> {} — seen, don't answer")
            return self._reply(200, {})
        if lowered == "ping":
            answer = "pong"
        elif lowered == "time":
            answer = f"It is {datetime.now():%H:%M:%S} on the endpoint."
        else:
            answer = f"You said: {text}"

        show(f"      -> {answer!r}")
        self._reply(200, {"reply": answer})

    def do_GET(self):  # noqa: N802
        self._reply(200, {"ok": True, "received": seen})

    def _reply(self, status: int, payload):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


if __name__ == "__main__":
    mode = "SILENT (answers empty)" if SILENT else "replying"
    show(f"test endpoint on http://127.0.0.1:{PORT}/hook - {mode}")
    show("waiting for wadam to POST a message...")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
