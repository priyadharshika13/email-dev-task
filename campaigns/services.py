"""
Email Campaign Utilities
------------------------

Purpose:
    Helper functions for:
        - Processing uploaded recipient CSV files.
        - Enqueuing recipients for campaigns.
        - Sending scheduled or immediate campaign emails.
        - Generating and emailing campaign reports.

Key Responsibilities:
    - Validate and upsert Recipient records from CSV.
    - Link subscribed recipients to Campaigns via CampaignRecipient.
    - Send campaign emails in batches (scheduled or ad-hoc).
    - Generate admin reports (CSV + plain-text summaries) via email.

Assumptions:
    - Models: Campaign, Recipient, CampaignRecipient exist and are migrated.
    - EMAIL_BACKEND and related email settings are configured correctly.
    - ADMIN_REPORT_EMAIL is defined in Django settings for report delivery.
"""

import csv
import io
import logging
import smtplib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection, send_mail
from django.core.validators import validate_email
from django.utils import timezone

from .models import Campaign, Recipient, CampaignRecipient

logger = logging.getLogger(__name__)


def process_recipient_csv(file):
    """
    Process an uploaded CSV file and upsert Recipient records.

    Expected CSV columns:
        name, email, subscription_status

    Args:
        file: A Django UploadedFile or file-like object supporting .read().

    Returns:
        dict: {
            "created": int,
            "updated": int,
            "skipped": int,
            "invalid_emails": list[str],
            "recipients": list[Recipient],
        }

    Raises:
        ValidationError: If the file cannot be decoded or parsed at all.
    """
    try:
        # Read and decode the uploaded file
        raw = file.read()
        try:
            decoded_lines = raw.decode("utf-8").splitlines()
        except Exception as exc:
            logger.error("Failed to decode recipient CSV file: %s", exc, exc_info=True)
            raise ValidationError("Unable to decode CSV file as UTF-8.") from exc

        reader = csv.DictReader(decoded_lines)

        created = 0
        updated = 0
        skipped = 0
        invalid_emails: list[str] = []
        recipients: list[Recipient] = []

        for idx, row in enumerate(reader, start=1):
            try:
                name = (row.get("name") or "").strip()
                email = (row.get("email") or "").strip()
                status = (row.get("subscription_status") or "").strip().lower()
            except Exception as exc:
                logger.warning(
                    "Error parsing row %s in CSV: %s. Row will be skipped.",
                    idx,
                    exc,
                    exc_info=True,
                )
                skipped += 1
                invalid_emails.append(f"(row {idx}: parse error)")
                continue

            if not email:
                skipped += 1
                invalid_emails.append("(empty email)")
                continue

            # Email validation
            try:
                validate_email(email)
            except ValidationError:
                skipped += 1
                invalid_emails.append(email)
                continue

            if status not in ["subscribed", "unsubscribed"]:
                status = "subscribed"

            try:
                obj, created_flag = Recipient.objects.update_or_create(
                    email=email,
                    defaults={
                        "name": name,
                        "subscription_status": status,
                    },
                )
            except Exception as exc:
                logger.error(
                    "Error saving recipient '%s' from CSV: %s", email, exc, exc_info=True
                )
                skipped += 1
                invalid_emails.append(f"{email} (save error)")
                continue

            recipients.append(obj)
            if created_flag:
                created += 1
            else:
                updated += 1

        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "invalid_emails": invalid_emails,
            "recipients": recipients,  # 👈 important for grouping
        }

    except ValidationError:
        # Let caller handle validation failures
        raise
    except Exception as exc:
        logger.error("Unexpected error while processing recipient CSV: %s", exc, exc_info=True)
        raise ValidationError("Unexpected error while processing CSV file.") from exc


def _send_single_email(subject, body, to_email, html=False, connection=None):
    """
    Internal helper to send a single email.

    Args:
        subject (str): Email subject.
        body (str): Email body (plain text or HTML string).
        to_email (str): Recipient email address.
        html (bool): If True, body is treated as HTML.
        connection: Optional email backend connection.

    Raises:
        Exception: Any error raised by the email backend.
    """
    msg = EmailMultiAlternatives(
        subject=subject,
        body=body if not html else "",
        to=[to_email],
        connection=connection,
    )
    if html:
        msg.attach_alternative(body, "text/html")
    msg.send()


def process_due_campaigns(now=None, batch_size=100):
    """
    Process all campaigns that are due and send pending emails in batches.

    Logic:
        - Find campaigns with scheduled_time <= now and not COMPLETED.
        - For each campaign:
            * Mark status as IN_PROGRESS if it was DRAFT/SCHEDULED.
            * Send up to `batch_size` PENDING CampaignRecipient emails.
            * On exhaustion of PENDING recipients, mark campaign COMPLETED
              and trigger a summary report email.

    Args:
        now (datetime, optional): Reference time (defaults to timezone.now()).
        batch_size (int): Max number of emails to send per campaign per run.
    """
    now = now or timezone.now()

    try:
        due_campaigns = Campaign.objects.filter(
            scheduled_time__lte=now,
        ).exclude(
            status=Campaign.Status.COMPLETED,
        )
    except Exception as exc:
        logger.error("Error querying due campaigns: %s", exc, exc_info=True)
        return

    try:
        connection = get_connection()  # reuse SMTP connection
    except Exception as exc:
        logger.error("Failed to obtain email connection: %s", exc, exc_info=True)
        return

    for campaign in due_campaigns:
        try:
            # Mark in-progress
            if campaign.status in [Campaign.Status.DRAFT, Campaign.Status.SCHEDULED]:
                campaign.status = Campaign.Status.IN_PROGRESS
                campaign.save(update_fields=["status"])

            pending_qs = campaign.campaign_recipients.filter(
                status=CampaignRecipient.Status.PENDING
            )[:batch_size]

            if not pending_qs.exists():
                # No more pending; mark as completed & trigger report
                if campaign.status != Campaign.Status.COMPLETED:
                    campaign.status = Campaign.Status.COMPLETED
                    campaign.save(update_fields=["status"])
                    send_campaign_report(campaign)
                continue

            for cr in pending_qs:
                try:
                    _send_single_email(
                        subject=campaign.subject,
                        body=campaign.content,
                        to_email=cr.recipient_email_snapshot,
                        html=True,
                        connection=connection,
                    )
                    cr.status = CampaignRecipient.Status.SENT
                    cr.sent_at = now
                    cr.failure_reason = ""
                except Exception as e:
                    logger.error(
                        "Error sending email to %s for campaign %s: %s",
                        cr.recipient_email_snapshot,
                        campaign.id,
                        e,
                        exc_info=True,
                    )
                    cr.status = CampaignRecipient.Status.FAILED
                    cr.failure_reason = str(e)[:500]

                cr.save(update_fields=["status", "sent_at", "failure_reason"])

        except Exception as exc:
            logger.error(
                "Unexpected error while processing campaign %s: %s",
                campaign.id,
                exc,
                exc_info=True,
            )


def send_campaign_report(campaign: Campaign):
    """
    Generate a CSV summary of CampaignRecipient statuses and email it to admin.

    - Skips if `campaign.admin_report_sent` is already True.
    - Attaches a CSV file with per-recipient status.
    - Marks `admin_report_sent` = True only on successful send.

    Args:
        campaign (Campaign): Campaign instance to report on.
    """
    if campaign.admin_report_sent:
        return

    admin_email = getattr(settings, "ADMIN_REPORT_EMAIL", None)
    if not admin_email:
        logger.warning("ADMIN_REPORT_EMAIL is not configured; skipping campaign report.")
        return

    try:
        rows = []
        headers = ["Recipient Email", "Status", "Failure Reason", "Sent At"]
        for cr in campaign.campaign_recipients.all():
            rows.append(
                [
                    cr.recipient_email_snapshot,
                    cr.status,
                    cr.failure_reason,
                    cr.sent_at.isoformat() if cr.sent_at else "",
                ]
            )

        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(headers)
        writer.writerows(rows)
        csv_content = csv_buffer.getvalue()

        email = EmailMultiAlternatives(
            subject=f"Campaign Report: {campaign.name}",
            body=(
                f"Summary for campaign '{campaign.name}':\n"
                f"Total: {campaign.total_recipients}, "
                f"Sent: {campaign.sent_count}, "
                f"Failed: {campaign.failed_count}"
            ),
            to=[admin_email],
        )
        email.attach(
            filename=f"campaign_{campaign.id}_report.csv",
            content=csv_content,
            mimetype="text/csv",
        )
        email.send()

        campaign.admin_report_sent = True
        campaign.save(update_fields=["admin_report_sent"])

    except Exception as exc:
        logger.error(
            "Failed to send campaign CSV report for campaign %s: %s",
            campaign.id,
            exc,
            exc_info=True,
        )


def enqueue_recipients_for_campaign(campaign: Campaign) -> int:
    """
    Link subscribed recipients to this campaign based on assigned groups.

    Behavior:
        - Start with all Recipients whose subscription_status = 'subscribed'.
        - If the campaign has groups, filter recipients to only those groups.
        - For each matching Recipient, ensure a CampaignRecipient exists
          (idempotent: uses get_or_create).

    Args:
        campaign (Campaign): Campaign instance.

    Returns:
        int: Number of CampaignRecipient records created in this call.
    """
    try:
        # base query: only subscribed
        qs = Recipient.objects.filter(subscription_status="subscribed")

        # if campaign has groups, restrict to those groups
        if campaign.groups.exists():
            qs = qs.filter(groups__in=campaign.groups.all()).distinct()

        created = 0
        for r in qs:
            try:
                obj, is_created = CampaignRecipient.objects.get_or_create(
                    campaign=campaign,
                    recipient=r,
                    defaults={
                        "recipient_email_snapshot": r.email,
                        "status": CampaignRecipient.Status.PENDING,
                    },
                )
                if is_created:
                    created += 1
            except Exception as exc:
                logger.error(
                    "Error linking recipient %s to campaign %s: %s",
                    r.email,
                    campaign.id,
                    exc,
                    exc_info=True,
                )
                continue

        return created

    except Exception as exc:
        logger.error(
            "Unexpected error in enqueue_recipients_for_campaign for campaign %s: %s",
            campaign.id if campaign else "N/A",
            exc,
            exc_info=True,
        )
        return 0


def send_campaign_failure_report(campaign: Campaign, failed_details: list):
    """
    Email a simple failure report (only failures) to ADMIN_REPORT_EMAIL.

    Args:
        campaign (Campaign): Campaign instance.
        failed_details (list[dict]): Each dict has keys {"email", "reason"}.
    """
    if not failed_details:
        return

    admin_email = getattr(settings, "ADMIN_REPORT_EMAIL", None)
    if not admin_email:
        logger.warning("ADMIN_REPORT_EMAIL is not configured; skipping failure report.")
        return

    lines = [
        f"Campaign: {campaign.name} (ID: {campaign.id})",
        "",
        "The following recipients failed:",
        "",
    ]
    for fd in failed_details:
        lines.append(f"- {fd.get('email')}: {fd.get('reason')}")

    body = "\n".join(lines)

    try:
        send_mail(
            subject=f"[Campaign Failure Report] {campaign.name}",
            message=body,
            from_email=None,  # DEFAULT_FROM_EMAIL
            recipient_list=[admin_email],
        )
    except Exception as exc:
        logger.error(
            "Failed to send campaign failure report for campaign %s: %s",
            campaign.id,
            exc,
            exc_info=True,
        )


def send_campaign_now(campaign: Campaign, batch_size: int = 500):
    """
    Send all non-SENT recipients for this campaign (up to batch_size).

    - Adds a [CID:<id>] prefix to subject to help IMAP bounce processing.
    - Sends summary report via `send_campaign_failure_report_email`.

    Args:
        campaign (Campaign): Campaign instance to send.
        batch_size (int): Maximum number of entries to process.

    Returns:
        tuple[int, int]: (sent_count, failed_count)
    """
    admin_email = getattr(settings, "ADMIN_REPORT_EMAIL", None)
    try:
        connection = get_connection()
    except Exception as exc:
        logger.error("Failed to obtain email connection for immediate send: %s", exc, exc_info=True)
        # still send report if possible
        send_campaign_failure_report_email(
            campaign, sent=0, failed=0, total=0, failed_details=[]
        )
        return 0, 0

    try:
        pending_qs = campaign.campaign_recipients.exclude(
            status=CampaignRecipient.Status.SENT
        )[:batch_size]
    except Exception as exc:
        logger.error(
            "Error querying pending recipients for campaign %s: %s",
            campaign.id,
            exc,
            exc_info=True,
        )
        send_campaign_failure_report_email(
            campaign, sent=0, failed=0, total=0, failed_details=[]
        )
        return 0, 0

    total = pending_qs.count()
    if not total:
        # still send a report: nothing to send
        send_campaign_failure_report_email(
            campaign, sent=0, failed=0, total=0, failed_details=[]
        )
        return 0, 0

    now = timezone.now()
    sent = 0
    failed = 0
    failed_details = []

    # tag subject with CID for bounce processing
    tagged_subject = f"[CID:{campaign.id}] {campaign.subject}"

    for cr in pending_qs:
        try:
            msg = EmailMultiAlternatives(
                subject=tagged_subject,
                body="",
                to=[cr.recipient_email_snapshot],
                connection=connection,
            )
            msg.extra_headers = {
                "X-Campaign-ID": str(campaign.id),
            }
            msg.attach_alternative(campaign.content or "<p>No content</p>", "text/html")
            msg.send()

            cr.status = CampaignRecipient.Status.SENT
            cr.sent_at = now
            cr.failure_reason = ""
            sent += 1
        except Exception as e:
            reason = str(e)[:500]
            logger.error(
                "Error sending immediate campaign email to %s (campaign %s): %s",
                cr.recipient_email_snapshot,
                campaign.id,
                e,
                exc_info=True,
            )
            cr.status = CampaignRecipient.Status.FAILED
            cr.failure_reason = reason
            failed += 1
            failed_details.append(
                {
                    "email": cr.recipient_email_snapshot,
                    "reason": reason,
                }
            )

        cr.save(update_fields=["status", "sent_at", "failure_reason"])

    # 🔔 send summary report to admin after this send
    send_campaign_failure_report_email(
        campaign, sent=sent, failed=failed, total=total, failed_details=failed_details
    )

    return sent, failed


def send_campaign_failure_report_email(campaign, sent, failed, total, failed_details):
    """
    Send a detailed summary report (success + failures) to ADMIN_REPORT_EMAIL.

    Args:
        campaign (Campaign): Campaign instance.
        sent (int): Number of successful sends.
        failed (int): Number of failed sends.
        total (int): Total attempted recipients in this run.
        failed_details (list[dict]): Each dict has {"email", "reason"}.
    """
    admin_email = getattr(settings, "ADMIN_REPORT_EMAIL", None)
    if not admin_email:
        logger.warning("ADMIN_REPORT_EMAIL is not configured; skipping summary report email.")
        return

    lines = [
        "Campaign Report",
        "---------------------------",
        f"Name      : {campaign.name}",
        f"ID        : {campaign.id}",
        f"Subject   : {campaign.subject}",
        f"Scheduled : {campaign.scheduled_time}",
        f"Triggered : {timezone.now()}",
        "",
        f"Total recipients considered : {total}",
        f"Sent successfully           : {sent}",
        f"Failed during send          : {failed}",
        "",
    ]

    if failed_details:
        lines.append("Failed recipients:")
        for item in failed_details:
            lines.append(f"- {item.get('email')}: {item.get('reason')}")
    else:
        lines.append("No immediate SMTP failures recorded during this run.")

    body = "\n".join(lines)

    try:
        send_mail(
            subject=f"[Campaign Report] {campaign.name} (Sent: {sent}, Failed: {failed})",
            message=body,
            from_email=None,  # uses DEFAULT_FROM_EMAIL
            recipient_list=[admin_email],
        )
    except Exception as exc:
        logger.error(
            "Failed to send summary report email for campaign %s: %s",
            campaign.id,
            exc,
            exc_info=True,
        )

def test_smtp_credentials(username: str, password: str) -> tuple[bool, str | None]:
    """
    Try to connect & login to the SMTP server with given credentials.
    Returns (success, error_message_or_None).
    """
    host = getattr(settings, "EMAIL_HOST", "smtp.gmail.com")
    port = getattr(settings, "EMAIL_PORT", 587)
    use_tls = getattr(settings, "EMAIL_USE_TLS", True)

    try:
        server = smtplib.SMTP(host, port, timeout=10)
        if use_tls:
            server.starttls()
        server.login(username, password)
        server.quit()
        return True, None
    except Exception as exc:
        return False, str(exc)