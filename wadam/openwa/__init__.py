"""The OpenWA transport.

This package replaces `wadam/whatsapp/` — roughly four thousand lines that
drove WhatsApp Desktop's accessibility tree, because there was no API. There is
one now. Everything that layer existed to do, OpenWA does over HTTP:

    finding the window, forcing it foreground          → gone
    clicking a sidebar row realized off-screen         → gone
    filling a contenteditable that rejects SetValue    → gone
    counting outgoing bubbles to prove delivery        → gone
    hashing a chat's display name for an id            → gone, OpenWA has real ids
    hashing message content to deduplicate             → gone, OpenWA has message ids

What is left is a client that POSTs a message and a receiver that accepts
webhook deliveries.
"""

from wadam.openwa.client import OpenWAClient, SendError
from wadam.openwa.inbound import InboundMessage, parse_delivery, verify_signature

__all__ = [
    "OpenWAClient",
    "SendError",
    "InboundMessage",
    "parse_delivery",
    "verify_signature",
]
