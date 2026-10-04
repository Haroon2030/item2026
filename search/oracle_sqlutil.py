"""مساعدات SQL مشتركة لاستعلامات أوراكل."""

from __future__ import annotations


def num_bind(placeholder: str) -> str:
    """يحوّل bind نصياً (مثل ':wh') إلى رقم على جانب القيمة لا العمود.

    يُبقي الفهرس قابلاً للاستخدام، والقيمة غير الرقمية تعطي NULL (لا تطابق شيئاً)
    بدل ORA-01722. أوراكل 12.1 لا يدعم DEFAULT ... ON CONVERSION ERROR.
    """
    b = placeholder if placeholder.startswith(":") else f":{placeholder}"
    return f"CASE WHEN REGEXP_LIKE({b}, '^ *[0-9]+ *$') THEN TO_NUMBER({b}) END"
