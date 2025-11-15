from django.core.management.base import BaseCommand
from campaigns.imap_bounce_processor import process_bounce_messages


class Command(BaseCommand):
    help = "Process IMAP bounce emails and mark failed campaign recipients."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mailbox",
            type=str,
            default="INBOX",
            help="IMAP mailbox to scan, e.g. INBOX or [Gmail]/Spam",
        )

    def handle(self, *args, **options):
        mailbox = options["mailbox"]
        self.stdout.write(self.style.NOTICE(f"Processing bounce messages from {mailbox}..."))
        process_bounce_messages(mailbox=mailbox)
        self.stdout.write(self.style.SUCCESS("Bounce processing completed."))
