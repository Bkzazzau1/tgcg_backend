"""Re-read Ask Me's source pages; mark the items of any page that changed.

    python manage.py check_askme_sources

Run weekly (systemd timer or cron). A changed page's items keep answering,
marked for a reviewer to compare with the new page. Prints counts only.
"""

from django.core.management.base import BaseCommand

from apps.askme import services
from apps.askme.models import SourcePage


class Command(BaseCommand):
    help = "Re-read Ask Me's source pages and mark items whose page changed."

    def handle(self, *args, **options):
        outcomes = {"same": 0, "changed": 0, "failed": 0}
        for page in SourcePage.objects.select_related("site").order_by("pk").iterator():
            outcomes[services.recheck_page(page)] += 1
        self.stdout.write(
            f"Pages unchanged {outcomes['same']}, changed {outcomes['changed']}, "
            f"could not be read {outcomes['failed']}."
        )
