# campaigns/tasks.py

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .imap_bounce_processor import process_bounce_messages
from .services import process_due_campaigns, enqueue_recipients_for_campaign
from .imap_bounce_processor import process_bounce_messages

from celery import shared_task
from .services import process_due_campaigns, send_campaign_now
from .models import Campaign


@shared_task
def process_due_campaigns_task():
    process_due_campaigns()


@shared_task
def send_campaign_now_task(campaign_id: int):
    try:
        campaign = Campaign.objects.get(pk=campaign_id)
    except Campaign.DoesNotExist:
        return
    send_campaign_now(campaign)

@shared_task
def check_bounces_task(mailbox="INBOX"):
    process_bounce_messages(mailbox=mailbox)

@shared_task
def process_scheduled_campaigns():
    """
    Periodic task:
    - Find all campaigns with status='scheduled' and scheduled_time <= now
    - Enqueue recipients
    - Send emails
    - Update status to 'completed' (or 'in_progress' while sending)
    """
    print(timezone.localtime(timezone.now()))
    now = timezone.localtime(timezone.now())

    # pick all due campaigns
    due_campaigns = Campaign.objects.filter(
        status="scheduled",
        scheduled_time__lte=now,
    )

    print("test")
    print(now)
    print(Campaign.objects.filter(
        status="scheduled",
        scheduled_time__lte=now,
    ))
    for campaign in due_campaigns:
        with transaction.atomic():
            # mark as in_progress
            campaign.status = "in_progress"
            campaign.save(update_fields=["status"])
            print("progress")

            # attach recipients (subscribed only)
            created_links = enqueue_recipients_for_campaign(campaign)

            # send emails (synchronous inside this task)
            sent, failed = send_campaign_now(campaign)

            # if everything attempted, mark as completed
            campaign.status = "completed"
            campaign.save(update_fields=["status"])

            # (optional) you can log or create an audit record here
            print(
                f"[AUTO] Campaign {campaign.id} ('{campaign.name}') "
                f"processed: recipients added={created_links}, sent={sent}, failed={failed}"
            )

            process_bounces_for_campaign.apply_async(
                args=[campaign.id],
                countdown=120,  # 2 min later
            )

@shared_task
def process_bounces_for_campaign(campaign_id=None):
    """
    Run IMAP bounce processing and mark failed recipients.

    If your imap_bounce_processor supports campaign_id filtering, pass it through.
    Otherwise you can ignore campaign_id in process_bounce_messages and just
    process all recent bounces.
    """
    process_bounce_messages(mailbox="INBOX")


