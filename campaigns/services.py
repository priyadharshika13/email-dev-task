import csv
import io
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection, send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from .models import Campaign, Recipient, CampaignRecipient


def process_recipient_csv(file):
    decoded = file.read().decode("utf-8").splitlines()
    reader = csv.DictReader(decoded)

    created = 0
    updated = 0
    skipped = 0
    invalid_emails = []
    recipients = []

    for row in reader:
        name = row.get("name", "").strip()
        email = row.get("email", "").strip()
        status = row.get("subscription_status", "").strip().lower()

        if not email:
            skipped += 1
            invalid_emails.append("(empty email)")
            continue

        try:
            validate_email(email)
        except ValidationError:
            skipped += 1
            invalid_emails.append(email)
            continue

        if status not in ["subscribed", "unsubscribed"]:
            status = "subscribed"

        obj, created_flag = Recipient.objects.update_or_create(
            email=email,
            defaults={
                "name": name,
                "subscription_status": status,
            },
        )
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
        "recipients": recipients,      # 👈 important for grouping
    }


def enqueue_recipients_for_campaign(campaign: Campaign):
    """
    Populate CampaignRecipient rows for all subscribed recipients.
    Run when campaign is moved to Scheduled.
    """
    recipients = Recipient.objects.filter(subscription_status="subscribed")
    existing_pairs = CampaignRecipient.objects.filter(campaign=campaign).values_list(
        "recipient_id", flat=True
    )
    existing_set = set(existing_pairs)

    to_create = []
    for recipient in recipients:
        if recipient.id in existing_set:
            continue
        to_create.append(
            CampaignRecipient(
                campaign=campaign,
                recipient=recipient,
                recipient_email_snapshot=recipient.email,
                status=CampaignRecipient.Status.PENDING,
            )
        )
    CampaignRecipient.objects.bulk_create(to_create, batch_size=1000)


def _send_single_email(subject, body, to_email, html=False, connection=None):
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
    Called by management command periodically.
    - Picks campaigns whose scheduled_time <= now and not completed.
    - Sends emails in batches for each campaign.
    """
    now = now or timezone.now()

    due_campaigns = Campaign.objects.filter(
        scheduled_time__lte=now,
    ).exclude(
        status=Campaign.Status.COMPLETED,
    )

    connection = get_connection()  # reuse SMTP connection

    for campaign in due_campaigns:
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
                cr.status = CampaignRecipient.Status.FAILED
                cr.failure_reason = str(e)[:500]
            cr.save(update_fields=["status", "sent_at", "failure_reason"])


def send_campaign_report(campaign: Campaign):
    """
    Generate CSV summary and email to admin.
    """
    if campaign.admin_report_sent:
        return

    rows = []
    headers = ["Recipient Email", "Status", "Failure Reason", "Sent At"]
    for cr in campaign.campaign_recipients.all():
        rows.append([
            cr.recipient_email_snapshot,
            cr.status,
            cr.failure_reason,
            cr.sent_at.isoformat() if cr.sent_at else "",
        ])

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    csv_content = csv_buffer.getvalue()

    admin_email = getattr(settings, "ADMIN_REPORT_EMAIL", None)
    if not admin_email:
        return

    email = EmailMultiAlternatives(
        subject=f"Campaign Report: {campaign.name}",
        body=f"Summary for campaign '{campaign.name}':\n"
             f"Total: {campaign.total_recipients}, "
             f"Sent: {campaign.sent_count}, "
             f"Failed: {campaign.failed_count}",
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


def enqueue_recipients_for_campaign(campaign: Campaign) -> int:
    """
    Link subscribed recipients to this campaign based on assigned groups.
    Returns number of CampaignRecipient records created.
    """
    # base query: only subscribed
    qs = Recipient.objects.filter(subscription_status="subscribed")

    # if campaign has groups, restrict to those groups
    if campaign.groups.exists():
        qs = qs.filter(groups__in=campaign.groups.all()).distinct()

    created = 0
    for r in qs:
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
    return created

def send_campaign_failure_report(campaign: Campaign, failed_details: list):
    """
    Email a simple failure report to ADMIN_REPORT_EMAIL.
    failed_details: list of dicts -> {"email": ..., "reason": ...}
    """
    if not failed_details:
        return

    admin_email = getattr(settings, "ADMIN_REPORT_EMAIL", None)
    if not admin_email:
        return

    lines = [
        f"Campaign: {campaign.name} (ID: {campaign.id})",
        "",
        "The following recipients failed:",
        "",
    ]
    for fd in failed_details:
        lines.append(f"- {fd['email']}: {fd['reason']}")

    body = "\n".join(lines)

    send_mail(
        subject=f"[Campaign Failure Report] {campaign.name}",
        message=body,
        from_email=None,  # DEFAULT_FROM_EMAIL
        recipient_list=[admin_email],
    )


def send_campaign_now(campaign: Campaign, batch_size: int = 500):
    """
    Send all pending recipients for this campaign (up to batch_size).
    Returns (sent_count, failed_count).
    Also emails a report to ADMIN_REPORT_EMAIL.
    """
    connection = get_connection()

    pending_qs = campaign.campaign_recipients.exclude(
        status=CampaignRecipient.Status.SENT
    )[:batch_size]

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
            cr.status = CampaignRecipient.Status.FAILED
            cr.failure_reason = reason
            failed += 1
            failed_details.append({
                "email": cr.recipient_email_snapshot,
                "reason": reason,
            })

        cr.save(update_fields=["status", "sent_at", "failure_reason"])

    # 🔔 send summary report to admin after this send
    send_campaign_failure_report_email(
        campaign, sent=sent, failed=failed, total=total, failed_details=failed_details
    )

    return sent, failed

def send_campaign_failure_report_email(campaign, sent, failed, total, failed_details):
    """
    Sends an error/summary report to ADMIN_REPORT_EMAIL for this campaign.
    failed_details = list of dicts: {"email": ..., "reason": ...}
    """
    admin_email = getattr(settings, "ADMIN_REPORT_EMAIL", None)
    if not admin_email:
        return

    lines = [
        f"Campaign Report",
        f"---------------------------",
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
            lines.append(f"- {item['email']}: {item['reason']}")
    else:
        lines.append("No immediate SMTP failures recorded during this run.")

    body = "\n".join(lines)

    send_mail(
        subject=f"[Campaign Report] {campaign.name} (Sent: {sent}, Failed: {failed})",
        message=body,
        from_email=None,  # uses DEFAULT_FROM_EMAIL
        recipient_list=[admin_email],
    )