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
    - RecipientGroup exists to categorise recipients
"""

from django import forms
from django.core.exceptions import ValidationError
from .models import Campaign, Recipient, RecipientGroup


class CampaignForm(forms.ModelForm):

    # HTML content textarea remains same
    content = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "materialize-textarea code-area",
                "rows": 12,
                "placeholder": (
                    "<h1>Welcome!</h1>\n"
                    "<p>Thanks for joining us.</p>"
                ),
            }
        ),
        help_text="Plain text or HTML allowed."
    )

    # 🔥 CUSTOM MULTIPLE-SELECT FIELD WITH “All Recipients” OPTION
    groups = forms.MultipleChoiceField(
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "browser-default"}),
        help_text="Select groups. Choose 'All Recipients' to target everyone.",
    )

    class Meta:
        model = Campaign
        fields = ["name", "subject", "content", "scheduled_time", "status", "groups"]
        widgets = {
            "scheduled_time": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "status": forms.Select(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Fetch real DB groups
        try:
            group_choices = [(str(g.id), g.name) for g in RecipientGroup.objects.all()]
        except Exception:
            group_choices = []

        # 🔥 Insert virtual ALL option at the top
        self.fields["groups"].choices = [
            ("__all__", "🌐 All Recipients"),
        ] + group_choices

    def clean_content(self):
        content = self.cleaned_data.get("content", "")
        if "<script" in content.lower():
            raise ValidationError("Script tags are not allowed.")
        return content



class RecipientUploadForm(forms.Form):
    """
    Form for uploading a CSV file containing bulk recipient data.

    Expected CSV Format:
        name,email,subscription_status

    Example:
        John Doe,john@example.com,subscribed
        Jane Smith,jane@example.com,unsubscribed

    Fields:
        file (FileField): The uploaded CSV file.
        group (ModelChoiceField): Existing group to attach recipients to.
        new_group_name (CharField): Optional new group name.

    Notes:
        - Clean method wrapped in exception handling.
        - Ensures either group or new_group_name is provided.
    """

    file = forms.FileField(
        help_text="Upload CSV with columns: name,email,subscription_status"
    )

    try:
        group = forms.ModelChoiceField(
            queryset=RecipientGroup.objects.all(),
            required=False,
            help_text="Existing group to attach these recipients to.",
        )
    except Exception as exc:
        group = forms.ModelChoiceField(
            queryset=RecipientGroup.objects.none(),
            required=False,
            help_text="Groups unavailable due to system error.",
        )
        print("Warning: Could not load RecipientGroup in RecipientUploadForm:", exc)

    new_group_name = forms.CharField(
        required=False,
        help_text="Or enter a new group name to create and attach recipients.",
    )

    def clean(self):
        """
        Validate form input ensuring at least one of:
            - existing group, OR
            - new group name
        is provided.

        Returns:
            dict: cleaned form data

        Raises:
            ValidationError: On invalid or missing input.
        """
        try:
            cleaned = super().clean()

            group = cleaned.get("group")
            new_group_name = cleaned.get("new_group_name", "").strip()

            if not group and not new_group_name:
                raise ValidationError(
                    "Please select an existing group or enter a new group name."
                )

            return cleaned

        except ValidationError:
            # Re-raise known validation errors
            raise
        except Exception as exc:
            # Wrap any unexpected errors
            raise ValidationError(f"Unexpected validation error: {exc}")

class AdminEmailConfigForm(forms.Form):
    admin_email = forms.EmailField(
        label="Admin Report Email",
        help_text="All campaign error / bounce reports will be sent here.",
    )
    smtp_email = forms.EmailField(
        label="SMTP Email (Login)",
        help_text="Your SMTP login email (e.g., Gmail address).",
    )
    smtp_app_password = forms.CharField(
        label="SMTP App Password",
        widget=forms.PasswordInput(render_value=True),
        help_text="Use an App Password (for Gmail) instead of your normal password.",
    )
