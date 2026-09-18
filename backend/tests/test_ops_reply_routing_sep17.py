"""Reply-by-email when the ops mailbox cannot be polled (Sep 17).

Quintin moved to a Google Workspace address (`quintin@takeshapehome.com`).
Workspace admins can withhold app passwords and switch IMAP off for the
domain, so the mailbox that receives the lead is not necessarily one we can
read. `LIDARAI_OPS_REPLY_TO` separates the two: ops receives the lead at
their own address and replies normally, the reply lands in a mailbox we do
poll, and the sender check still requires the message to come from ops.
"""

from __future__ import annotations

import email
from email.message import EmailMessage

from app.config import settings
from app.flow import ops_email, ops_reply


def test_reply_to_defaults_to_the_ops_address(monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "ops@example.com")
    monkeypatch.setattr(settings, "ops_reply_to", "")
    assert ops_email.reply_to_address() == "ops@example.com"


def test_reply_to_moves_the_destination_without_moving_the_recipient(monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "quintin@takeshapehome.com")
    monkeypatch.setattr(settings, "ops_reply_to", "leads@sudo-rndm.example")
    assert ops_email.reply_to_address() == "leads@sudo-rndm.example"


def test_the_sent_message_carries_that_reply_to(monkeypatch):
    sent: dict[str, str] = {}

    class _SMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def send_message(self, msg):
            sent["reply_to"] = msg["Reply-To"]
            sent["to"] = msg["To"]

    monkeypatch.setattr(settings, "ops_reply_enabled", True)
    monkeypatch.setattr(settings, "ops_email", "quintin@takeshapehome.com")
    monkeypatch.setattr(settings, "ops_reply_to", "leads@sudo-rndm.example")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "leads@sudo-rndm.example")
    monkeypatch.setattr(settings, "smtp_password", "x")
    monkeypatch.setattr(ops_email.smtplib, "SMTP", _SMTP)

    ops_email._send_smtp("quintin@takeshapehome.com", "subject", "body")

    # The lead still goes to ops; only the reply destination moved.
    assert sent["to"] == "quintin@takeshapehome.com"
    assert sent["reply_to"] == "leads@sudo-rndm.example"


def test_only_ops_may_send_a_reply(monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "quintin@takeshapehome.com")
    monkeypatch.setattr(settings, "ops_reply_senders", "")
    allowed = ops_reply.allowed_reply_senders()
    assert allowed == {"quintin@takeshapehome.com"}
    assert "someone-else@example.com" not in allowed


def test_a_second_ops_account_can_be_allowed(monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "quintin@takeshapehome.com")
    monkeypatch.setattr(
        settings, "ops_reply_senders", "quintin.lunsford@gmail.com, Quintin@Example.com "
    )
    allowed = ops_reply.allowed_reply_senders()
    assert allowed == {
        "quintin@takeshapehome.com",
        "quintin.lunsford@gmail.com",
        "quintin@example.com",
    }


def test_the_reply_mailbox_itself_is_not_a_trusted_sender(monkeypatch):
    """The poller reads our mailbox; mail *we* put there is not a quote."""
    monkeypatch.setattr(settings, "ops_email", "quintin@takeshapehome.com")
    monkeypatch.setattr(settings, "ops_reply_to", "leads@sudo-rndm.example")
    monkeypatch.setattr(settings, "ops_reply_senders", "")
    assert "leads@sudo-rndm.example" not in ops_reply.allowed_reply_senders()


def test_a_workspace_reply_parses_as_a_quote(monkeypatch):
    """End to end on the message shape: ops replies from the Workspace
    address into our mailbox and the body survives the fetch."""
    msg = EmailMessage()
    msg["From"] = "Quintin Lunsford <quintin@takeshapehome.com>"
    msg["To"] = "leads@sudo-rndm.example"
    msg["Subject"] = "Re: New quote request qr_1840571464c8 - Flooring"
    msg.set_content(
        "Brightline Painting came back at $2,450, valid 30 days.\n\n"
        "On Wed, Sep 17, 2026, TakeShape Ops wrote:\n> the original lead\n"
    )
    parsed = email.message_from_bytes(msg.as_bytes())
    body = ops_reply.strip_quoted_history(ops_reply._plain_text_body(parsed))
    assert "Brightline Painting came back at $2,450" in body
    assert "the original lead" not in body
