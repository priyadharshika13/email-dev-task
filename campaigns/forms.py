"""
Forms Module for Campaign Management
------------------------------------

Purpose:
    Contains all Django form classes related to creating campaigns and uploading
    recipient data. Supports rich HTML email content and CSV uploads.

Included Forms:
    - CampaignForm: ModelForm for creating and editing email campaigns.
    - RecipientUploadForm: Standard form for uploading recipient CSV files.

Usage:
    These forms are used in admin-like views for handling campaign setup and
    bulk recipient management.

Assumptions:
    - Campaign model contains (name, subject, content, scheduled_time, status)
    - Recipient model will be validated on upload
"""

from django import forms
from .models import Campaign, Recipient, RecipientGroup


class CampaignForm(forms.ModelForm):
    """
    Form for creating and editing email Campaign objects.

    Features:
        - Rich textarea for HTML email content.
        - Supports plain text or full HTML templates.
        - Allows scheduling using datetime-local widget.
        - Ensures minimal friction when composing marketing or transactional emails.

    Fields:
        name (str): Campaign title.
        subject (str): Email subject line.
        content (str): Email body in text or HTML.
        scheduled_time (datetime): When campaign should trigger.
        status (enum): Draft / Scheduled / In Progress / Completed.

    Notes:
        The content field shows a placeholder HTML example, demonstrating
        supported markup including inline images via public URLs.
    """

    content = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "materialize-textarea code-area",
                "rows": 12,
                "placeholder": (
                    "<h1>Welcome!</h1>\n"
                    "<p>Thanks for joining our platform.</p>\n"
                    "<img src='https://example.com/banner.png' alt='Banner' />"
                ),
            }
        ),
        help_text="You can type plain text or paste HTML here. Images should use public image URLs.",
    )

    groups = forms.ModelMultipleChoiceField(
        queryset=RecipientGroup.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "browser-default"}),
        help_text="Select one or more recipient groups. If empty, campaign uses all subscribed recipients.",
    )

    class Meta:
        model = Campaign
        fields = ["name", "subject", "content", "scheduled_time", "status", "groups"]
        widgets = {
            "scheduled_time": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "status": forms.Select(),
        }

class RecipientUploadForm(forms.Form):
    """
    Form for uploading a CSV file containing bulk recipient data.

    Expected CSV Format:
        name,email,subscription_status

    Example:
        John Doe,john@example.com,subscribed
        Jane Smith,jane@example.com,unsubscribed

    Fields:
        file (FileField): CSV file uploaded by admin/user.

    Notes:
        - Validation of CSV structure and email format should be done inside the view.
        - Duplicate emails must be handled gracefully in the importer.
    """

    file = forms.FileField(
        help_text="Upload CSV with columns: name,email,subscription_status"
    )
    group = forms.ModelChoiceField(
        queryset=RecipientGroup.objects.all(),
        required=False,
        help_text="Existing group to attach these recipients to.",
    )
    new_group_name = forms.CharField(
        required=False,
        help_text="Or type a new group name to create and attach recipients.",
    )

    def clean(self):
        cleaned = super().clean()
        group = cleaned.get("group")
        new_group_name = cleaned.get("new_group_name", "").strip()

        if not group and not new_group_name:
            raise forms.ValidationError(
                "Please select an existing group or enter a new group name."
            )
        return cleaned

