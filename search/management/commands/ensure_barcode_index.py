from django.core.management.base import BaseCommand

from search.api_client import ApiClientError, sync_barcode_index
from search.models import ItemBarcode


class Command(BaseCommand):
    help = 'يزامن فهرس الباركود تلقائياً إذا كان فارغاً (للعبوة/الرصيد).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='إعادة المزامنة حتى لو الفهرس غير فارغ',
        )

    def handle(self, *args, **options):
        count = ItemBarcode.objects.count()
        if count and not options['force']:
            self.stdout.write(self.style.SUCCESS(f'الفهرس جاهز ({count} سجل) — لا حاجة للمزامنة.'))
            return

        self.stdout.write('الفهرس فارغ — بدء مزامنة GetAllItems…')
        try:
            synced = sync_barcode_index()
        except ApiClientError as exc:
            self.stderr.write(self.style.ERROR(f'فشلت المزامنة التلقائية: {exc}'))
            return

        with_pack = ItemBarcode.objects.exclude(pack_size='').count()
        self.stdout.write(
            self.style.SUCCESS(f'تمت المزامنة: {synced} سجل (عبوات معبأة: {with_pack})')
        )
