"""
Campaign Views Module
---------------------

Purpose:
    Provides Django views for managing email campaigns, recipients,
    bounce reports, and manual trigger actions.

Includes:
    - Dashboard with high-level stats.
    - CRUD for Campaign.
    - Recipient CSV upload & basic recipient maintenance.
    - Manual campaign trigger (immediate send + bounce check scheduling).
    - Bounce listing and CSV export.

Assumptions:
    - Models: Campaign, Recipient, CampaignRecipient, BounceRecord, RecipientGroup.
    - Services: process_recipient_csv, enqueue_recipients_for_campaign, send_campaign_now.
    - Tasks: process_due_campaigns_task, check_bounces_task, send_campaign_now_task (Celery).
"""

import csv
import logging

from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib import messages

from bulk_email_system import settings
from .models import Campaign, Recipient, CampaignRecipient, BounceRecord, RecipientGroup
from .forms import CampaignForm, RecipientUploadForm, AdminEmailConfigForm
from .services import process_recipient_csv, enqueue_recipients_for_campaign, send_campaign_now, test_smtp_credentials
from .tasks import process_due_campaigns_task, check_bounces_task, send_campaign_now_task  # Celery tasks

logger = logging.getLogger(__name__)


# ---------- DASHBOARD (MAIN PAGE WITH SIDEBAR) ----------

def dashboard(request):
    """
    Render the dashboard with global campaign stats and recent campaigns.

    Shows:
        - Total campaigns and counts by status.
        - Total recipients, sent, and failed counts.
        - Last 5 campaigns with aggregated stats.
    """
    stats = {}
    recent_campaigns = []

    try:
        # Global stats
        stats["total"] = Campaign.objects.count()
        stats["scheduled"] = Campaign.objects.filter(status="scheduled").count()
        stats["in_progress"] = Campaign.objects.filter(status="in_progress").count()
        stats["completed"] = Campaign.objects.filter(status="completed").count()

        stats["total_recipients"] = CampaignRecipient.objects.count()
        stats["total_sent"] = CampaignRecipient.objects.filter(
            status=CampaignRecipient.Status.SENT
        ).count()
        stats["total_failed"] = CampaignRecipient.objects.filter(
            status=CampaignRecipient.Status.FAILED
        ).count()

        # Recent campaigns with per-campaign counts
        recent_campaigns = (
            Campaign.objects
            .annotate(
                total_recipients_=Count("campaign_recipients", distinct=True),
                sent_count_=Count(
                    "campaign_recipients",
                    filter=Q(campaign_recipients__status=CampaignRecipient.Status.SENT),
                    distinct=True,
                ),
                failed_count_=Count(
                    "campaign_recipients",
                    filter=Q(campaign_recipients__status=CampaignRecipient.Status.FAILED),
                    distinct=True,
                ),
            )
            .order_by("-created_at")[:5]
        )
    except Exception as exc:
        logger.error("Error loading dashboard stats: %s", exc, exc_info=True)
        messages.error(request, "Unable to load full dashboard stats at this time.")

    return render(
        request,
        "campaigns/dashboard.html",
        {
            "stats": stats,
            "recent_campaigns": recent_campaigns,
        },
    )


# ---------- LIST / DETAIL ----------

def campaign_list(request):
    """
    List all campaigns with aggregated recipient, sent, and failed counts.
    """
    campaigns = []
    try:
        campaigns = Campaign.objects.annotate(
            total_recipients=Count("campaign_recipients"),
            sent_count=Count(
                "campaign_recipients",
                filter=Q(campaign_recipients__status="sent"),
            ),
            failed_count=Count(
                "campaign_recipients",
                filter=Q(campaign_recipients__status="failed"),
            ),
        ).order_by("-created_at")
    except Exception as exc:
        logger.error("Error loading campaign list: %s", exc, exc_info=True)
        messages.error(request, "Unable to load campaigns list.")

    return render(request, "campaigns/campaign_list.html", {"campaigns": campaigns})


def campaign_detail(request, pk):
    """
    Show details for a single campaign, including its recipients and status counts.

    Args:
        pk (int): Primary key of the Campaign.

    Raises:
        404: If the Campaign does not exist.
    """
    campaign = get_object_or_404(Campaign, pk=pk)

    recipients = []
    total = sent = failed = pending = 0

    try:
        recipients = campaign.campaign_recipients.select_related("recipient").order_by(
            "-created_at"
        )

        total = recipients.count()
        sent = recipients.filter(status=CampaignRecipient.Status.SENT).count()
        failed = recipients.filter(status=CampaignRecipient.Status.FAILED).count()
        pending = recipients.filter(status=CampaignRecipient.Status.PENDING).count()
    except Exception as exc:
        logger.error("Error loading campaign detail for %s: %s", pk, exc, exc_info=True)
        messages.error(request, "Unable to load full campaign details.")

    return render(
        request,
        "campaigns/campaign_detail.html",
        {
            "campaign": campaign,
            "recipients": recipients,
            "total": total,
            "sent": sent,
            "failed": failed,
            "pending": pending,
        },
    )


# ---------- CREATE / UPDATE / DELETE ----------


def _normalized_post_for_groups(request):
    """
    Helper: copy POST and normalize 'groups' so that when the special
    '__all__' option is selected, we clear all group IDs.

    This makes the campaign have NO explicit groups, which your
    enqueue_recipients_for_campaign can interpret as
    'send to ALL subscribed recipients'.
    """
    post_data = request.POST.copy()
    selected_groups = post_data.getlist("groups")

    if "__all__" in selected_groups:
        # Clear all group selections so campaign.groups is empty.
        # This lets existing logic treat it as "all recipients".
        post_data.setlist("groups", [])

    return post_data


def campaign_create(request):
    """
    Create a new Campaign.

    Behavior:
        - On POST: validates and saves CampaignForm.
        - If status is SCHEDULED, enqueues recipients immediately.
        - Special case: if the UI sent a '__all__' option in groups,
          we clear groups so the campaign targets ALL recipients.
    """
    if request.method == "POST":
        # 🔥 Normalize groups: handle '__all__'
        normalized_post = _normalized_post_for_groups(request)
        form = CampaignForm(normalized_post)

        if form.is_valid():
            try:
                campaign = form.save()
                if campaign.status == Campaign.Status.SCHEDULED:
                    enqueue_recipients_for_campaign(campaign)
                messages.success(request, "Campaign created.")
                return redirect("campaigns:campaign_list")
            except Exception as exc:
                logger.error("Error creating campaign: %s", exc, exc_info=True)
                messages.error(request, "Failed to create campaign. Please try again.")
    else:
        form = CampaignForm()

    return render(
        request,
        "campaigns/campaign_form.html",
        {"form": form, "mode": "create"},
    )


def campaign_update(request, pk):
    """
    Update an existing Campaign.

    Args:
        pk (int): Campaign primary key.

    Behavior:
        - On POST: validates and saves CampaignForm.
        - If status is SCHEDULED, enqueues recipients.
        - Special case: if '__all__' selected in groups, clear all groups
          so this campaign again targets ALL recipients.
    """
    campaign = get_object_or_404(Campaign, pk=pk)

    if request.method == "POST":
        # 🔥 Normalize groups: handle '__all__'
        normalized_post = _normalized_post_for_groups(request)
        form = CampaignForm(normalized_post, instance=campaign)

        if form.is_valid():
            try:
                campaign = form.save()
                if campaign.status == Campaign.Status.SCHEDULED:
                    enqueue_recipients_for_campaign(campaign)
                messages.success(request, "Campaign updated.")
                return redirect("campaigns:campaign_detail", pk=campaign.pk)
            except Exception as exc:
                logger.error("Error updating campaign %s: %s", pk, exc, exc_info=True)
                messages.error(request, "Failed to update campaign. Please try again.")
    else:
        form = CampaignForm(instance=campaign)

    return render(
        request,
        "campaigns/campaign_form.html",
        {"form": form, "mode": "update", "campaign": campaign},
    )


def campaign_delete(request, pk):
    """
    Delete a Campaign after confirmation.

    Args:
        pk (int): Campaign primary key.
    """
    campaign = get_object_or_404(Campaign, pk=pk)

    if request.method == "POST":
        try:
            campaign.delete()
            messages.success(request, "Campaign deleted.")
            return redirect("campaigns:campaign_list")
        except Exception as exc:
            logger.error("Error deleting campaign %s: %s", pk, exc, exc_info=True)
            messages.error(request, "Failed to delete campaign. Please try again.")

    return render(
        request,
        "campaigns/campaign_delete_confirm.html",
        {"campaign": campaign},
    )


# ---------- AUTO TRIGGER (MANUAL) ----------

def campaign_trigger_now(request, pk):
    """
    Trigger immediate sending for a campaign.

    Steps:
        1. Enqueue subscribed recipients for this campaign.
        2. Send emails immediately (up to batch size) via `send_campaign_now`.
        3. Schedule a bounce check (Celery) after 2 minutes.
        4. Show a summary message.

    Args:
        pk (int): Campaign primary key.
    """
    campaign = get_object_or_404(Campaign, pk=pk)

    try:
        created_links = enqueue_recipients_for_campaign(campaign)
    except Exception as exc:
        logger.error(
            "Error enqueuing recipients for campaign %s: %s", campaign.id, exc, exc_info=True
        )
        messages.error(request, "Failed to enqueue recipients for this campaign.")
        return redirect("campaigns:campaign_detail", pk=campaign.pk)

    try:
        sent, failed = send_campaign_now(campaign)
    except Exception as exc:
        logger.error(
            "Error sending campaign %s immediately: %s", campaign.id, exc, exc_info=True
        )
        messages.error(request, "Failed to send campaign immediately.")
        sent, failed = 0, 0

    # schedule bounce check after 2 minutes
    try:
        check_bounces_task.apply_async(countdown=120)
    except Exception as exc:
        logger.error("Error scheduling bounce check task: %s", exc, exc_info=True)
        messages.warning(request, "Campaign sent, but bounce check could not be scheduled.")

    messages.success(
        request,
        (
            f"Triggered sending for '{campaign.name}'. "
            f"Recipients added: {created_links}, Sent: {sent}, Failed (SMTP): {failed}. "
            f"Bounce scan will run in ~2 minutes."
        ),
    )
    return redirect("campaigns:campaign_detail", pk=campaign.pk)


# ---------- RECIPIENT UPLOAD / EDIT / DELETE ----------

def recipient_upload(request):
    """
    Upload a CSV of recipients and optionally attach them to a group.

    Behavior:
        - Validates RecipientUploadForm.
        - Processes CSV via `process_recipient_csv`.
        - Creates a new group if `new_group_name` is provided.
        - Attaches created/updated recipients to the chosen group.
        - Shows invalid email list as a message.
        - Renders recent recipients and campaigns for convenience.
    """
    if request.method == "POST":
        form = RecipientUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                file = form.cleaned_data["file"]
                group = form.cleaned_data.get("group")
                new_group_name = form.cleaned_data.get("new_group_name", "").strip()

                if new_group_name:
                    group, _ = RecipientGroup.objects.get_or_create(name=new_group_name)

                summary = process_recipient_csv(file)
                recipients_created_or_updated = summary.get("recipients", [])
                invalid_emails = summary.get("invalid_emails", [])

                if invalid_emails:
                    messages.warning(
                        request,
                        f"Some records were skipped due to invalid emails: {invalid_emails}",
                    )

                if group and recipients_created_or_updated:
                    for r in recipients_created_or_updated:
                        try:
                            r.groups.add(group)
                        except Exception as exc:
                            logger.error(
                                "Error attaching recipient %s to group %s: %s",
                                r.id,
                                group.id,
                                exc,
                                exc_info=True,
                            )

                messages.success(
                    request,
                    "Recipient CSV processed successfully.",
                )
                return redirect("campaigns:recipient_upload")
            except Exception as exc:
                logger.error("Error handling recipient upload: %s", exc, exc_info=True)
                messages.error(request, "Failed to process uploaded recipients.")
    else:
        form = RecipientUploadForm()

    # ALL recipients, with all groups prefetched
    try:
        recipients = (
            Recipient.objects
            .prefetch_related("groups")
            .order_by("-created_at")
        )
    except Exception as exc:
        logger.error("Error loading recipients list: %s", exc, exc_info=True)
        recipients = []
        messages.error(request, "Unable to load recipients list.")

    # campaigns for the dropdown
    try:
        campaigns = Campaign.objects.order_by("-created_at")
    except Exception as exc:
        logger.error("Error loading campaigns for recipient upload page: %s", exc, exc_info=True)
        campaigns = []
        messages.error(request, "Unable to load campaigns for assignment.")

    return render(
        request,
        "campaigns/recipient_upload.html",
        {
            "form": form,
            "recipients": recipients,
            "campaigns": campaigns,
        },
    )


def recipient_edit(request, pk):
    """
    Edit a single Recipient.

    NOTE:
        RecipientUploadForm is not a ModelForm, so using `instance=recipient`
        will normally raise a TypeError. Here we guard against that and show a
        friendly message if direct editing is not wired up.
    """
    recipient = get_object_or_404(Recipient, pk=pk)

    if request.method == "POST":
        try:
            # This will likely fail since RecipientUploadForm is a plain Form.
            form = RecipientUploadForm(request.POST, request.FILES)
            if form.is_valid():
                # You can customize this part to map fields manually if needed.
                cleaned = form.cleaned_data
                recipient.email = cleaned.get("file") or recipient.email  # placeholder
                recipient.save()
                messages.success(request, "Recipient updated successfully.")
                return redirect("campaigns:recipient_upload")
        except TypeError:
            messages.error(
                request,
                "Inline editing is not configured for recipients with this form.",
            )
            return redirect("campaigns:recipient_upload")
        except Exception as exc:
            logger.error("Error updating recipient %s: %s", pk, exc, exc_info=True)
            messages.error(request, "Failed to update recipient.")
    else:
        # For now, just show a simple form without instance-mapping
        try:
            form = RecipientUploadForm()
        except Exception as exc:
            logger.error("Error loading recipient edit form: %s", exc, exc_info=True)
            messages.error(request, "Unable to load recipient edit form.")
            form = RecipientUploadForm()

    return render(
        request,
        "campaigns/recipient_edit.html",
        {"form": form, "recipient": recipient},
    )


def recipient_delete(request, pk):
    """
    Delete a Recipient after confirmation.

    Args:
        pk (int): Recipient primary key.
    """
    recipient = get_object_or_404(Recipient, pk=pk)

    if request.method == "POST":
        try:
            recipient.delete()
            messages.success(request, "Recipient deleted successfully.")
            return redirect("campaigns:recipient_upload")
        except Exception as exc:
            logger.error("Error deleting recipient %s: %s", pk, exc, exc_info=True)
            messages.error(request, "Failed to delete recipient. Please try again.")

    return render(
        request,
        "campaigns/recipient_delete_confirm.html",
        {"recipient": recipient},
    )


# ---------- BOUNCE LIST & REPORT ----------

def bounce_list(request):
    """
    Display a list of bounce records, optionally filtered by campaign_id.

    Query params:
        ?campaign_id=<id>
    """
    try:
        qs = BounceRecord.objects.select_related("campaign")

        # optional filters by campaign
        campaign_id = request.GET.get("campaign_id")
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
    except Exception as exc:
        logger.error("Error loading bounce list: %s", exc, exc_info=True)
        messages.error(request, "Unable to load bounce records.")
        qs = BounceRecord.objects.none()

    return render(request, "campaigns/bounce_list.html", {"bounces": qs})


def bounce_report_csv(request):
    """
    Export bounce records as a CSV file.

    Query params:
        ?campaign_id=<id>   (optional filter)

    Columns:
        Campaign ID, Campaign Name, Recipient Email, Reason, Message ID, Processed At
    """
    try:
        qs = BounceRecord.objects.select_related("campaign").order_by("-processed_at")

        # optional filter by campaign
        campaign_id = request.GET.get("campaign_id")
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
    except Exception as exc:
        logger.error("Error querying bounce records for CSV: %s", exc, exc_info=True)
        messages.error(request, "Unable to generate bounce report.")
        return redirect("campaigns:bounce_list")

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="bounce_report.csv"'

    try:
        writer = csv.writer(response)
        writer.writerow(
            [
                "Campaign ID",
                "Campaign Name",
                "Recipient Email",
                "Reason",
                "Message ID",
                "Processed At",
            ]
        )

        for b in qs:
            writer.writerow(
                [
                    b.campaign_id,
                    b.campaign.name if b.campaign else "",
                    b.recipient_email,
                    b.reason,
                    b.message_id,
                    b.processed_at.isoformat() if b.processed_at else "",
                ]
            )
    except Exception as exc:
        logger.error("Error writing bounce CSV: %s", exc, exc_info=True)
        messages.error(request, "Error generating CSV report.")
        return redirect("campaigns:bounce_list")

    return response

def is_staff_or_superuser(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def email_settings_view(request):
    """
    Admin form to configure:
      - ADMIN_REPORT_EMAIL
      - EMAIL_HOST_USER
      - EMAIL_HOST_PASSWORD

    Behavior:
      - Prefills with current settings.
      - On POST, tests SMTP credentials.
      - If test OK: updates settings.* at runtime.
      - If test fails: keeps existing settings, shows error.
    """
    # Current values from settings.py (effective values)
    current_admin = getattr(settings, "ADMIN_REPORT_EMAIL", "")
    current_smtp_email = getattr(settings, "EMAIL_HOST_USER", "")
    # We never show current password; field will be empty by default.

    if request.method == "POST":
        form = AdminEmailConfigForm(request.POST)
        if form.is_valid():
            new_admin_email = form.cleaned_data["admin_email"]
            new_smtp_email = form.cleaned_data["smtp_email"]
            new_app_password = form.cleaned_data["smtp_app_password"]

            # 1️⃣ Test the new SMTP credentials
            ok, err = test_smtp_credentials(new_smtp_email, new_app_password)

            if not ok:
                # ❌ Invalid → keep existing settings, show error
                form.add_error(
                    None,
                    f"SMTP connection/login failed. Existing settings kept. Details: {err}",
                )
            else:
                # ✅ Valid → update runtime settings
                settings.ADMIN_REPORT_EMAIL = new_admin_email
                settings.EMAIL_HOST_USER = new_smtp_email
                settings.EMAIL_HOST_PASSWORD = new_app_password
                settings.DEFAULT_FROM_EMAIL = new_smtp_email

                messages.success(
                    request,
                    "Email settings updated successfully and tested OK."
                )
                return redirect("campaigns:email_settings")
    else:
        form = AdminEmailConfigForm(
            initial={
                "admin_email": current_admin,
                "smtp_email": current_smtp_email,
            }
        )

    return render(request, "campaigns/email_settings.html", {"form": form})