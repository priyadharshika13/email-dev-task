from django.core.validators import validate_email
from django.db import models
from django.utils import timezone

class RecipientGroup(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
class Recipient(models.Model):
    class SubscriptionStatus(models.TextChoices):
        SUBSCRIBED = "subscribed", "Subscribed"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"

    name = models.CharField(max_length=255)
    email = models.EmailField(
        unique=True,
        validators=[validate_email]
    )
    subscription_status = models.CharField(
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.SUBSCRIBED,
    )
    groups = models.ManyToManyField(
        RecipientGroup,
        related_name="recipients",
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.email} ({self.subscription_status})"



class Campaign(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SCHEDULED = "scheduled", "Scheduled"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    name = models.CharField(max_length=255)
    subject = models.CharField(max_length=255)
    content = models.TextField(help_text="Plain text or HTML")
    scheduled_time = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    groups = models.ManyToManyField(
        RecipientGroup,
        related_name="campaigns",
        blank=True,
        help_text="Select which recipient groups this campaign should be sent to.",

    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    admin_report_sent = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} ({self.status})"

    def total_recipients(self):
        return self.campaign_recipients.count()


    def sent_count(self):
        return self.campaign_recipients.filter(status=CampaignRecipient.Status.SENT).count()


    def failed_count(self):
        return self.campaign_recipients.filter(status=CampaignRecipient.Status.FAILED).count()

    @property
    def status_summary(self):
        return f"{self.sent_count}/{self.total_recipients} sent"


# campaigns/models.py
class CampaignRecipient(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="campaign_recipients",
    )
    recipient = models.ForeignKey(
        Recipient,
        on_delete=models.CASCADE,
        related_name="campaigns",
    )
    recipient_email_snapshot = models.EmailField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

class BounceRecord(models.Model):
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="bounces",
    )
    recipient_email = models.EmailField()
    reason = models.TextField(blank=True)
    message_id = models.CharField(max_length=255, blank=True)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-processed_at"]

    def __str__(self):
        return f"Bounce: {self.recipient_email} (campaign {self.campaign_id})"




