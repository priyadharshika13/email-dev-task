from django.core.management.base import BaseCommand
from campaigns.services import process_due_campaigns


class Command(BaseCommand):
    help = "Process scheduled campaigns and send pending emails."

    def handle(self, *args, **options):
        process_due_campaigns()
        self.stdout.write(self.style.SUCCESS("Processed campaigns"))
