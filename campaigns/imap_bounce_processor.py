# campaigns/imap_bounce_processor.py

"""
IMAP Bounce Processor
---------------------

Purpose:
    Connects to an IMAP inbox (typically a Gmail or SMTP bounce inbox),
    scans for bounce messages, and marks affected recipients in the database
    as FAILED. It also records a BounceRecord for audit and reporting.

Key Responsibilities:
    - Connect to IMAP using Django settings (IMAP_*).
    - Search for bounce-like messages (from MAILER-DAEMON or similar subjects).
    - Parse each bounce email to:
        • Extract the original campaign ID from the subject (via [CID:<id>]).
        • Extract the failed recipient email address.
        • Extract a human-readable failure reason.
        • Persist bounce info to CampaignRecipient and BounceRecord.

Assumptions:
    - Settings contain:
        IMAP_HOST, IMAP_PORT, IMAP_USERNAME, IMAP_PASSWORD, IMAP_USE_SSL.
    - Campaign emails include a token in the subject like: "[CID:123] My Subject".
    - Models:
        - Campaign
        - CampaignRecipient (with recipient_email_snapshot, status, failure_reason)
        - BounceRecord (campaign, recipient_email, reason, message_id)

Typical Usage:
    - Invoked via a Django management command, e.g.:

        from campaigns.imap_bounce_processor import process_bounce_messages
        process_bounce_messages(mailbox="INBOX")

    - Can be scheduled via cron / Celery beat to periodically process bounces.
"""

import imaplib
import email
import re
from django.conf import settings
from .models import Campaign, CampaignRecipient, BounceRecord

# Pattern to extract campaign id from subject, e.g. "[CID:123]"
CID_PATTERN = re.compile(r"\[CID:(\d+)\]")

# Simple email matcher for fallback parsing
EMAIL_PATTERN = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")


def connect_imap() -> imaplib.IMAP4:
    """
    Establish and return an authenticated IMAP connection.

    Uses the following Django settings:
        - IMAP_HOST (default: "imap.gmail.com")
        - IMAP_PORT (default: 993)
        - IMAP_USERNAME
        - IMAP_PASSWORD
        - IMAP_USE_SSL (True/False)

    Returns:
        imaplib.IMAP4 or imaplib.IMAP4_SSL: Logged-in IMAP client instance.

    Raises:
        imaplib.IMAP4.error: If login fails or connection cannot be established.
    """
    host = getattr(settings, "IMAP_HOST", "imap.gmail.com")
    port = getattr(settings, "IMAP_PORT", 993)
    username = settings.IMAP_USERNAME
    password = settings.IMAP_PASSWORD

    if getattr(settings, "IMAP_USE_SSL", True):
        imap = imaplib.IMAP4_SSL(host, port)
    else:
        imap = imaplib.IMAP4(host, port)

    imap.login(username, password)
    return imap


def extract_campaign_id_from_subject(subject: str | None) -> int | None:
    """
    Extract the campaign ID from a subject line containing a [CID:<id>] token.

    Example:
        Subject: "[CID:42] Welcome to our platform"
        -> returns 42

    Args:
        subject (str | None): The email subject line.

    Returns:
        int | None: Campaign ID if found and parseable, otherwise None.
    """
    if not subject:
        return None
    m = CID_PATTERN.search(subject)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def extract_failed_recipient_from_message(msg: email.message.Message) -> str | None:
    """
    Attempt to detect the failed recipient's email address from a bounce message.

    Strategy:
        1) Look for "message/delivery-status" parts and parse "Final-Recipient".
        2) If not found, fall back to scanning plain-text parts for an email
           address, ignoring the sending (FROM) address.

    Args:
        msg (email.message.Message): Parsed email message object.

    Returns:
        str | None: The recipient email address that bounced, if detected.
    """
    # 1) Some DSNs have "Final-Recipient" header in the payload
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            payload = part.get_payload()
            if isinstance(payload, list):
                for p in payload:
                    final_recipient = p.get("Final-Recipient")
                    if final_recipient and "@" in final_recipient:
                        # often like "rfc822; someone@example.com"
                        pieces = final_recipient.split(";")
                        return pieces[-1].strip()

    # 2) Fallback: search body text for an email address
    body_text = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            try:
                body_text += part.get_payload(decode=True).decode(errors="ignore")
            except Exception:
                continue

    matches = EMAIL_PATTERN.findall(body_text)
    if matches:
        # pick the first email that is not your own sending address
        from_addr = getattr(settings, "EMAIL_HOST_USER", "").lower()
        for addr in matches:
            if addr.lower() != from_addr:
                return addr

    return None


def extract_original_subject(msg: email.message.Message) -> str | None:
    """
    Try to find the original subject inside the bounce.

    Many bounce messages attach the original email as `message/rfc822`.
    This function first checks for such an attachment and reads its Subject.
    If not found, it falls back to the bounce email's own Subject.

    Args:
        msg (email.message.Message): Parsed email message object.

    Returns:
        str | None: The original or best-guess subject line.
    """
    # 1) Check attached original message
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            try:
                payload = part.get_payload()[0]
                return payload.get("Subject")
            except Exception:
                continue

    # 2) Fallback: sometimes bounce subject itself keeps original subject
    return msg.get("Subject")


def mark_failed_recipient(
    campaign_id: int,
    recipient_email: str,
    failure_reason: str,
    message_id: str | None,
) -> None:
    """
    Mark a campaign recipient as FAILED and persist a BounceRecord.

    Steps:
        - Locate the Campaign by ID.
        - Locate all CampaignRecipient rows matching the bounced email.
        - Update their status to FAILED and store a truncated failure_reason.
        - Create a BounceRecord entry for analytics / reporting.

    Args:
        campaign_id (int): ID of the Campaign to which the email belonged.
        recipient_email (str): Email address that bounced.
        failure_reason (str): Short description (usually bounce subject or DSN info).
        message_id (str | None): Raw Message-ID of the bounce email (if available).

    Returns:
        None
    """
    try:
        campaign = Campaign.objects.get(pk=campaign_id)
    except Campaign.DoesNotExist:
        print(f"[IMAP] No campaign with id={campaign_id}")
        return

    cr_qs = campaign.campaign_recipients.filter(
        recipient_email_snapshot__iexact=recipient_email
    )

    if not cr_qs.exists():
        print(f"[IMAP] No CampaignRecipient found for {recipient_email} in campaign {campaign_id}")
        return

    for cr in cr_qs:
        cr.status = CampaignRecipient.Status.FAILED
        cr.failure_reason = failure_reason[:500]
        cr.save(update_fields=["status", "failure_reason"])
        print(f"[IMAP] Marked FAILED: campaign={campaign_id}, email={recipient_email}")

    # Store bounce record (1 per bounce email)
    BounceRecord.objects.create(
        campaign=campaign,
        recipient_email=recipient_email,
        reason=failure_reason[:2000],
        message_id=message_id or "",
    )


def process_bounce_messages(mailbox: str = "INBOX") -> None:
    """
    Scan the given IMAP mailbox for bounce messages and process them.

    Search Filter:
        - FROM "MAILER-DAEMON"
        - OR SUBJECT "Mail Delivery Subsystem"

    For each matching message:
        - Parse the raw email.
        - Extract original subject → campaign_id via [CID:<id>] tag.
        - Extract failed recipient email.
        - Extract a short failure_reason (bounce Subject).
        - Mark the recipient as FAILED and create BounceRecord.
        - Mark the IMAP message as \Seen.

    Args:
        mailbox (str): IMAP mailbox name to select (default: "INBOX").

    Returns:
        None
    """
    imap = connect_imap()
    imap.select(mailbox)

    status, data = imap.search(
        None,
        '(OR FROM "MAILER-DAEMON" SUBJECT "Mail Delivery Subsystem")'
    )

    if status != "OK":
        print("[IMAP] Search failed")
        imap.logout()
        return

    msg_ids = data[0].split()
    print(f"[IMAP] Found {len(msg_ids)} potential bounce messages")

    for msg_id in msg_ids:
        status, msg_data = imap.fetch(msg_id, "(RFC822)")
        if status != "OK":
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        orig_subject = extract_original_subject(msg)
        campaign_id = extract_campaign_id_from_subject(orig_subject)
        failed_email = extract_failed_recipient_from_message(msg)
        failure_reason = msg.get("Subject", "Delivery failed")
        message_id = msg.get("Message-ID", "")

        print(
            f"[IMAP] Processing bounce msg_id={msg_id}, "
            f"campaign_id={campaign_id}, email={failed_email}"
        )

        if campaign_id and failed_email:
            mark_failed_recipient(campaign_id, failed_email, failure_reason, message_id)

        # mark as seen
        imap.store(msg_id, "+FLAGS", "\\Seen")

    imap.close()
    imap.logout()
