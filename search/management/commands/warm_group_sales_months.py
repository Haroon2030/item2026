"""تدفئة/تحديث مبيعات المجموعات.

افتراضياً: عيّنة خفيفة (مناسبة لـ Hostinger/WAN).
مع --full: مسح DTL كامل لكل شهر (بطيء — للتشغيل قرب أوراكل أو ليلاً).
"""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "تدفئة كاش مبيعات المجموعات (عيّنة خفيفة أو مسح كامل)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=0,
            help="عدد الأيام للخلف من اليوم (0 = من أول السنة الحالية).",
        )
        parser.add_argument(
            "--from",
            dest="date_from",
            default="",
            help="تاريخ البداية YYYY-MM-DD",
        )
        parser.add_argument(
            "--to",
            dest="date_to",
            default="",
            help="تاريخ النهاية YYYY-MM-DD (افتراضي اليوم)",
        )
        parser.add_argument(
            "--full",
            action="store_true",
            help="مسح DTL كامل لكل شهر (بطيء عبر WAN).",
        )
        parser.add_argument(
            "--branch",
            default="",
            help="رمز فرع اختياري",
        )

    def handle(self, *args, **options):
        from search.oracle_stock import (
            _fetch_one_month_group_totals,
            _fetch_pos_group_totals_light,
            _groups_cache_key,
            _merge_available_monthly_group_cache,
            _month_spans,
            _sales_cache_set,
            oracle_enabled,
        )

        if not oracle_enabled():
            raise CommandError("أوراكل غير مفعّل.")

        today = date.today()
        raw_from = str(options.get("date_from") or "").strip()
        raw_to = str(options.get("date_to") or "").strip()
        days = int(options.get("days") or 0)
        force_full = bool(options.get("full"))
        brn = str(options.get("branch") or "").strip()

        if raw_from:
            date_from = date.fromisoformat(raw_from[:10])
        elif days > 0:
            date_from = today - timedelta(days=days - 1)
        else:
            date_from = date(today.year, 1, 1)
        date_to = date.fromisoformat(raw_to[:10]) if raw_to else today
        if date_to < date_from:
            raise CommandError("date_to قبل date_from")

        mode_label = "full" if force_full else "light"
        self.stdout.write(
            f"Groups warm {mode_label}: {date_from} → {date_to}"
            + (f" branch={brn}" if brn else "")
        )

        # فرض وضع المسح لفترة الأمر فقط
        prev = getattr(settings, "GROUPS_SQL_MODE", "light")
        try:
            if force_full:
                settings.GROUPS_SQL_MODE = "full"
                months = _month_spans(date_from, date_to)
                for a, b in months:
                    self.stdout.write(f"  full month {a} → {b} …")
                    rows = _fetch_one_month_group_totals(
                        system="pos",
                        date_from=a,
                        date_to=b,
                        brn=brn,
                        gcode="",
                        split_by_branch=False,
                        fast=True,
                    )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"    ok {len(rows or [])} groups"
                        )
                    )
                merged, missing = _merge_available_monthly_group_cache(
                    system="pos",
                    date_from=date_from,
                    date_to=date_to,
                    brn=brn,
                    gcode="",
                    split_by_branch=False,
                    mode="gross",
                )
                if merged is not None and not missing:
                    key = _groups_cache_key(
                        "pos", date_from, date_to, brn, "", False, "gross"
                    )
                    _sales_cache_set(
                        key, merged, date_from=date_from, date_to=date_to
                    )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"period cache ok ({len(merged)} groups)"
                        )
                    )
                elif missing:
                    self.stdout.write(
                        self.style.WARNING(
                            f"still missing {len(missing)} months"
                        )
                    )
            else:
                settings.GROUPS_SQL_MODE = "light"
                rows = _fetch_pos_group_totals_light(
                    date_from, date_to, branch_code=brn, group_code=""
                )
                key = _groups_cache_key(
                    "pos", date_from, date_to, brn, "", False, "gross"
                )
                _sales_cache_set(
                    key, rows, date_from=date_from, date_to=date_to
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        f"light sample ok: {len(rows or [])} groups → cache"
                    )
                )
        finally:
            settings.GROUPS_SQL_MODE = prev
