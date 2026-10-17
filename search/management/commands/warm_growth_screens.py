"""تدفئة كاش شاشتي «نمو مبيعات المجموعة بالأشهر» و«نمو مبيعات الفروع لهذه المجموعة».

يستدعي نفس الدوال التي تستدعيها الواجهة، فيملأ نفس مفاتيح الكاش:
- لكل مجموعة: نمو المجموعة (12 شهراً) ونمو الفروع (9 أشهر).
- الفترة الافتراضية: من --date-from (افتراضي أول السنة) إلى --date-to (افتراضي اليوم).

مثال يومي ليلاً:
    python manage.py warm_growth_screens
"""

from __future__ import annotations

import time
from datetime import date

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "تدفئة كاش نمو المبيعات (المجموعات والفروع) لكل مجموعة."

    def add_arguments(self, parser):
        parser.add_argument("--date-from", dest="date_from", default="", help="YYYY-MM-DD (افتراضي أول السنة)")
        parser.add_argument("--date-to", dest="date_to", default="", help="YYYY-MM-DD (افتراضي اليوم)")
        parser.add_argument("--groups", default="", help="رموز المجموعات مفصولة بفاصلة (افتراضي كلها)")
        parser.add_argument("--group-months", type=int, default=12)
        parser.add_argument("--branch-months", type=int, default=9)

    def handle(self, *args, **options):
        from search import runtime_config
        from search.oracle_stock import fetch_sales_group_options, oracle_enabled
        from search.sales_dashboard import build_sales_branch_growth, build_sales_group_growth

        # إعدادات الاتصال تُحمَّل من قاعدة التطبيق، والأمر لا يمرّ بطلب ويب
        runtime_config.refresh(force=True)
        if not oracle_enabled():
            raise CommandError("أوراكل غير مفعّل.")

        today = date.today()
        raw_from = str(options.get("date_from") or "").strip()
        raw_to = str(options.get("date_to") or "").strip()
        try:
            date_from = date.fromisoformat(raw_from[:10]) if raw_from else date(today.year, 1, 1)
            date_to = date.fromisoformat(raw_to[:10]) if raw_to else today
        except ValueError as exc:
            raise CommandError("صيغة التاريخ YYYY-MM-DD") from exc
        if date_to < date_from:
            raise CommandError("date_to قبل date_from")

        wanted = {g.strip() for g in str(options.get("groups") or "").split(",") if g.strip()}
        groups = [g for g in (fetch_sales_group_options() or []) if not wanted or g["code"] in wanted]
        self.stdout.write(f"Growth warm: {date_from} to {date_to}, groups={len(groups)}")

        failed = 0
        started = time.monotonic()
        for g in groups:
            code = g["code"]
            for label, fn, months in (
                ("group", build_sales_group_growth, options["group_months"]),
                ("branch", build_sales_branch_growth, options["branch_months"]),
            ):
                t0 = time.monotonic()
                try:
                    fn(date_to, branch_code="", months=months, group_code=code, date_from=date_from)
                    self.stdout.write(f"  {code} {label} ok ({time.monotonic() - t0:.0f}s)")
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    self.stdout.write(self.style.WARNING(f"  {code} {label} failed: {exc}"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Done in {time.monotonic() - started:.0f}s, failures={failed}"
            )
        )
