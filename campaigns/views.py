import csv

from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib import messages

from .models import Campaign, Recipient, CampaignRecipient, BounceRecord, RecipientGroup
from .forms import CampaignForm, RecipientUploadForm
from .services import process_recipient_csv, enqueue_recipients_for_campaign, send_campaign_now
from .tasks import process_due_campaigns_task, check_bounces_task  # Celery task
from .tasks import send_campaign_now_task


# ---------- DASHBOARD (MAIN PAGE WITH SIDEBAR) ----------

def dashboard(request):
  # Global stats
  stats = {}

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
          total_recipients_ = Count("campaign_recipients", distinct=True),
          sent_count_ = Count(
              "campaign_recipients",
              filter=Q(campaign_recipients__status=CampaignRecipient.Status.SENT),
              distinct=True,
          ),
          failed_count_ = Count(
              "campaign_recipients",
              filter=Q(campaign_recipients__status=CampaignRecipient.Status.FAILED),
              distinct=True,
          ),
      )
      .order_by("-created_at")[:5]
  )

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
    campaigns = Campaign.objects.annotate(
        total_recipients=Count("campaign_recipients"),
        sent_count=Count("campaign_recipients",
                         filter=Q(campaign_recipients__status="sent")),
        failed_count=Count("campaign_recipients",
                           filter=Q(campaign_recipients__status="failed")),
    ).order_by("-created_at")

    return render(request, "campaigns/campaign_list.html", {"campaigns": campaigns})


def campaign_detail(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    recipients = campaign.campaign_recipients.select_related("recipient").order_by("-created_at")

    total = recipients.count()
    sent = recipients.filter(status=CampaignRecipient.Status.SENT).count()
    failed = recipients.filter(status=CampaignRecipient.Status.FAILED).count()
    pending = recipients.filter(status=CampaignRecipient.Status.PENDING).count()

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

def campaign_create(request):
    if request.method == "POST":
        form = CampaignForm(request.POST)
        if form.is_valid():
            campaign = form.save()
            if campaign.status == Campaign.Status.SCHEDULED:
                enqueue_recipients_for_campaign(campaign)
            messages.success(request, "Campaign created.")
            return redirect("campaigns:campaign_list")
    else:
        form = CampaignForm()

    return render(request, "campaigns/campaign_form.html", {"form": form, "mode": "create"})


def campaign_update(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    if request.method == "POST":
        form = CampaignForm(request.POST, instance=campaign)
        if form.is_valid():
            campaign = form.save()
            if campaign.status == Campaign.Status.SCHEDULED:
                enqueue_recipients_for_campaign(campaign)
            messages.success(request, "Campaign updated.")
            return redirect("campaigns:campaign_detail", pk=campaign.pk)
    else:
        form = CampaignForm(instance=campaign)

    return render(
        request,
        "campaigns/campaign_form.html",
        {"form": form, "mode": "update", "campaign": campaign},
    )


def campaign_delete(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    if request.method == "POST":
        campaign.delete()
        messages.success(request, "Campaign deleted.")
        return redirect("campaigns:campaign_list")

    return render(request, "campaigns/campaign_delete_confirm.html", {"campaign": campaign})


# ---------- AUTO TRIGGER (MANUAL) ----------


def campaign_trigger_now(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)

    # Make sure recipients are attached
    enqueue_recipients_for_campaign(campaign)

    # Trigger Celery (or eager mode) to send this campaign immediately
    send_campaign_now_task.delay(campaign.id)

    messages.success(
        request,
        f"Sending triggered for campaign '{campaign.name}'."
    )
    return redirect("campaigns:campaign_detail", pk=campaign.pk)


# ---------- RECIPIENT UPLOAD ----------

def recipient_upload(request):
    if request.method == "POST":
        form = RecipientUploadForm(request.POST, request.FILES)
        if form.is_valid():
            file = form.cleaned_data["file"]
            group = form.cleaned_data.get("group")
            new_group_name = form.cleaned_data.get("new_group_name", "").strip()

            if new_group_name:
                group, _ = RecipientGroup.objects.get_or_create(name=new_group_name)

            summary = process_recipient_csv(file)
            recipients_created_or_updated = summary.get("recipients", [])

            if group and recipients_created_or_updated:
                for r in recipients_created_or_updated:
                    r.groups.add(group)

            # messages…
            return redirect("campaigns:recipient_upload")
    else:
        form = RecipientUploadForm()

    # ✅ ALL recipients, with all groups prefetched
    recipients = (
        Recipient.objects
        .prefetch_related("groups")
        .order_by("-created_at")
    )

    # campaigns for the dropdown
    campaigns = Campaign.objects.order_by("-created_at")

    return render(
        request,
        "campaigns/recipient_upload.html",
        {
            "form": form,
            "recipients": recipients,   # 👈 IMPORTANT
            "campaigns": campaigns,
        },
    )


def recipient_edit(request, pk):
    recipient = get_object_or_404(Recipient, pk=pk)

    if request.method == "POST":
        form = RecipientUploadForm(request.POST, instance=recipient)
        if form.is_valid():
            form.save()
            messages.success(request, "Recipient updated successfully.")
            return redirect("campaigns:recipient_upload")
    else:
        form = RecipientUploadForm(instance=recipient)

    return render(
        request,
        "campaigns/recipient_edit.html",
        {"form": form, "recipient": recipient},
    )

def recipient_delete(request, pk):
    recipient = get_object_or_404(Recipient, pk=pk)

    if request.method == "POST":
        recipient.delete()
        messages.success(request, "Recipient deleted successfully.")
        return redirect("campaigns:recipient_upload")

    return render(
        request,
        "campaigns/recipient_delete_confirm.html",
        {"recipient": recipient},
    )

#
# def campaign_trigger_now(request, pk):
#
#     campaign = get_object_or_404(Campaign, pk=pk)
#
#     # Trigger Celery task asynchronously
#     process_due_campaigns_task.delay()
#
#     messages.success(
#         request,
#         f"Auto-triggered background sending for campaign '{campaign.name}'."
#     )
#     return redirect("campaigns:campaign_detail", pk=campaign.pk)

def campaign_trigger_now(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)

    created_links = enqueue_recipients_for_campaign(campaign)
    sent, failed = send_campaign_now(campaign)

    # schedule bounce check after 2 minutes
    check_bounces_task.apply_async(countdown=120)

    messages.success(
        request,
        f"Triggered sending for '{campaign.name}'. "
        f"Recipients added: {created_links}, Sent: {sent}, Failed (SMTP): {failed}. "
        f"Bounce scan will run in ~2 minutes."
    )
    return redirect("campaigns:campaign_detail", pk=campaign.pk)

def bounce_list(request):
    qs = BounceRecord.objects.select_related("campaign")

    # optional filters by campaign
    campaign_id = request.GET.get("campaign_id")
    if campaign_id:
        qs = qs.filter(campaign_id=campaign_id)

    return render(request, "campaigns/bounce_list.html", {"bounces": qs})


def bounce_report_csv(request):
    qs = BounceRecord.objects.select_related("campaign").order_by("-processed_at")

    # optional filter by campaign
    campaign_id = request.GET.get("campaign_id")
    if campaign_id:
        qs = qs.filter(campaign_id=campaign_id)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="bounce_report.csv"'

    writer = csv.writer(response)
    writer.writerow(["Campaign ID", "Campaign Name", "Recipient Email", "Reason", "Message ID", "Processed At"])

    for b in qs:
        writer.writerow([
            b.campaign_id,
            b.campaign.name,
            b.recipient_email,
            b.reason,
            b.message_id,
            b.processed_at.isoformat(),
        ])

    return response
