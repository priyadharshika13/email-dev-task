# Auto-Generated Code Documentation

## 📄 `admin.py`
---
## 📄 `apps.py`
### Classes
#### CampaignsConfig
```
None
```

---
## 📄 `context_processors.py`
### Module Description
```
Context Processor: quarterly_planned_counts
------------------------------------------

Purpose:
    Provides aggregated counts of all *scheduled* email campaigns per financial
    quarter for the current year. This enables dashboards and analytics pages to
    quickly display upcoming workload distribution.

How It Works:
    - Fetches all Campaign objects with status `SCHEDULED`
    - Filters by current year (based on server timezone)
    - Groups the scheduled_time month into Q1, Q2, Q3, Q4 buckets

Returned Context:
    planned_quarter_counts = {
        "q1": <int>,   # Jan–Mar
        "q2": <int>,   # Apr–Jun
        "q3": <int>,   # Jul–Sep
        "q4": <int>,   # Oct–Dec
    }

Usage in Templates:
    {{ planned_quarter_counts.q1 }}
    {{ planned_quarter_counts.q2 }} etc.

Used For:
    - Admin dashboards
    - Quarterly email workload charts
    - Capacity planning reports

Assumptions:
    - Campaign model contains 'scheduled_time' datetime field
    - Campaign.Status.SCHEDULED is the enum for planned runs
```

### Functions
#### quarterly_planned_counts()
```
Compute scheduled email campaign counts by quarter for the current year.

Args:
    request (HttpRequest): The incoming request object. Required by Django
    for all context processors but not used internally.

Returns:
    dict: Dictionary containing:
        {
            "planned_quarter_counts": {
                "q1": int,  # Jan–Mar
                "q2": int,  # Apr–Jun
                "q3": int,  # Jul–Sep
                "q4": int,  # Oct–Dec
            }
        }

Notes:
    - This logic should remain lightweight, as context processors execute
      on every template render.
    - Designed for dashboard visualizations and reporting.
```

---
## 📄 `forms.py`
### Module Description
```
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
```

### Classes
#### CampaignForm
```
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
```

#### RecipientUploadForm
```
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
```

---
## 📄 `imap_bounce_processor.py`
### Module Description
```
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
```

### Functions
#### connect_imap()
```
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
```

#### extract_campaign_id_from_subject()
```
Extract the campaign ID from a subject line containing a [CID:<id>] token.

Example:
    Subject: "[CID:42] Welcome to our platform"
    -> returns 42

Args:
    subject (str | None): The email subject line.

Returns:
    int | None: Campaign ID if found and parseable, otherwise None.
```

#### extract_failed_recipient_from_message()
```
Attempt to detect the failed recipient's email address from a bounce message.

Strategy:
    1) Look for "message/delivery-status" parts and parse "Final-Recipient".
    2) If not found, fall back to scanning plain-text parts for an email
       address, ignoring the sending (FROM) address.

Args:
    msg (email.message.Message): Parsed email message object.

Returns:
    str | None: The recipient email address that bounced, if detected.
```

#### extract_original_subject()
```
Try to find the original subject inside the bounce.

Many bounce messages attach the original email as `message/rfc822`.
This function first checks for such an attachment and reads its Subject.
If not found, it falls back to the bounce email's own Subject.

Args:
    msg (email.message.Message): Parsed email message object.

Returns:
    str | None: The original or best-guess subject line.
```

#### mark_failed_recipient()
```
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
```

#### process_bounce_messages()
```
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
```

---
## 📄 `models.py`
### Classes
#### RecipientGroup
```
None
```

#### Recipient
```
None
```

#### Campaign
```
None
```

#### CampaignRecipient
```
None
```

#### BounceRecord
```
None
```

---
## 📄 `readme.py`
### Functions
#### extract_docstrings()
```
None
```

#### generate()
```
None
```

---
## 📄 `services.py`
### Functions
#### process_recipient_csv()
```
None
```

#### enqueue_recipients_for_campaign()
```
Populate CampaignRecipient rows for all subscribed recipients.
Run when campaign is moved to Scheduled.
```

#### _send_single_email()
```
None
```

#### process_due_campaigns()
```
Called by management command periodically.
- Picks campaigns whose scheduled_time <= now and not completed.
- Sends emails in batches for each campaign.
```

#### send_campaign_report()
```
Generate CSV summary and email to admin.
```

#### enqueue_recipients_for_campaign()
```
Link subscribed recipients to this campaign based on assigned groups.
Returns number of CampaignRecipient records created.
```

#### send_campaign_failure_report()
```
Email a simple failure report to ADMIN_REPORT_EMAIL.
failed_details: list of dicts -> {"email": ..., "reason": ...}
```

#### send_campaign_now()
```
Send all pending recipients for this campaign (up to batch_size).
Returns (sent_count, failed_count).
Also emails a report to ADMIN_REPORT_EMAIL.
```

#### send_campaign_failure_report_email()
```
Sends an error/summary report to ADMIN_REPORT_EMAIL for this campaign.
failed_details = list of dicts: {"email": ..., "reason": ...}
```

---
## 📄 `tasks.py`
### Functions
#### process_due_campaigns_task()
```
Celery task wrapper around process_due_campaigns().
```

#### process_due_campaigns_task()
```
None
```

#### send_campaign_now_task()
```
None
```

#### check_bounces_task()
```
None
```

#### process_scheduled_campaigns()
```
Periodic task:
- Find all campaigns with status='scheduled' and scheduled_time <= now
- Enqueue recipients
- Send emails
- Update status to 'completed' (or 'in_progress' while sending)
```

---
## 📄 `tests.py`
---
## 📄 `urls.py`
---
## 📄 `views.py`
### Functions
#### dashboard()
```
None
```

#### campaign_list()
```
None
```

#### campaign_detail()
```
None
```

#### campaign_create()
```
None
```

#### campaign_update()
```
None
```

#### campaign_delete()
```
None
```

#### campaign_trigger_now()
```
None
```

#### recipient_upload()
```
None
```

#### recipient_edit()
```
None
```

#### recipient_delete()
```
None
```

#### campaign_trigger_now()
```
None
```

#### bounce_list()
```
None
```

#### bounce_report_csv()
```
None
```

---
## 📄 `__init__.py`
---
## 📄 `process_bounces.py`
### Classes
#### Command
```
None
```

---
## 📄 `process_campaigns.py`
### Classes
#### Command
```
None
```

---
## 📄 `__init__.py`
---
## 📄 `0001_initial.py`
### Classes
#### Migration
```
None
```

---
## 📄 `0002_remove_campaignrecipient_campaigns_c_campaig_1a8cf7_idx_and_more.py`
### Classes
#### Migration
```
None
```

---
## 📄 `0003_alter_recipient_email.py`
### Classes
#### Migration
```
None
```

---
## 📄 `0004_bouncerecord.py`
### Classes
#### Migration
```
None
```

---
## 📄 `0005_recipientgroup.py`
### Classes
#### Migration
```
None
```

---
## 📄 `0006_campaign_groups_recipient_groups.py`
### Classes
#### Migration
```
None
```

---
## 📄 `__init__.py`
---
