"""
Django management command to process IMAP bounce emails.

This command connects to the configured IMAP mailbox and delegates
the actual processing to `campaigns.imap_bounce_processor.process_bounce_messages`.

Usage:
    python manage.py process_bounces --mailbox INBOX
    python manage.py process_bounces --mailbox "[Gmail]/Spam"
"""

from django.core.management.base import BaseCommand, CommandError
from campaigns.imap_bounce_processor import process_bounce_messages


class Command(BaseCommand):
    """
    Management command to process IMAP bounce emails and mark failed
    campaign recipients accordingly.
    """

    help = "Process IMAP bounce emails and mark failed campaign recipients."

    def add_arguments(self, parser):
        """
        Add custom command-line arguments for this management command.

        Args:
            parser: An argparse-style parser instance used by Django to
                    define command-line arguments.
        """
        parser.add_argument(
            "--mailbox",
            type=str,
            default="INBOX",
            help="IMAP mailbox to scan, e.g. INBOX or [Gmail]/Spam",
        )

    def handle(self, *args, **options):
        """
        Execute the management command.

        This method reads the mailbox argument, logs progress to stdout,
        and invokes the bounce processor. Any unexpected exceptions are
        caught and re-raised as a CommandError so that Django exits with
        a non-zero status code.

        Args:
            *args: Positional arguments passed to the command (unused).
            **options: Keyword arguments containing command options, e.g.
                       'mailbox' and 'verbosity'.
        """
        mailbox = options.get("mailbox", "INBOX")
        verbosity = int(options.get("verbosity", 1))

        try:
            self.stdout.write(
                self.style.NOTICE(f"Processing bounce messages from mailbox: {mailbox!r}...")
            )

            # If your processor returns a count, you can use it in the message.
            processed_count = process_bounce_messages(mailbox=mailbox)

            if processed_count is not None:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Bounce processing completed successfully. "
                        f"Processed {processed_count} message(s)."
                    )
                )
            else:
                # Fallback if the processor does not return anything
                self.stdout.write(
                    self.style.SUCCESS("Bounce processing completed successfully.")
                )

        except Exception as exc:
            # Log a clear error message and re-raise as CommandError
            self.stderr.write(
                self.style.ERROR(
                    "An unexpected error occurred while processing IMAP bounce messages."
                )
            )
            if verbosity > 1:
                # Optional: show full traceback in higher verbosity levels
                import traceback

                traceback.print_exc()

            raise CommandError(str(exc)) from exc
