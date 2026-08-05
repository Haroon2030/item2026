from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

SAR_SVG = (
    '<svg class="sar-symbol" viewBox="0 0 1124.14 1256.39" '
    'aria-label="ريال سعودي" role="img" focusable="false">'
    '<path fill="currentColor" d="M699.62,1113.02h0c-20.06,44.48-33.32,92.75-38.4,143.37l424.51-90.24c20.06-44.47,33.31-92.75,38.4-143.37l-424.51,90.24Z"/>'
    '<path fill="currentColor" d="M1085.73,895.8c20.06-44.47,33.32-92.75,38.4-143.37l-330.68,70.33v-135.2l292.27-62.11c20.06-44.47,33.32-92.75,38.4-143.37l-330.68,70.27V66.13c-50.67,28.45-95.67,66.32-132.25,110.99v403.35l-123.31,26.15V0c-50.67,28.44-95.67,66.32-132.25,110.99v525.69l-295.91,62.83c-20.06,44.47-33.33,92.75-38.42,143.37l334.33-71.05v170.26l-358.3,76.14c-20.06,44.47-33.32,92.75-38.4,143.37l375.04-79.7c30.53-6.35,56.77-24.4,73.83-50.9l36.68-30.52v92.57l-123.31,26.15v-92.57l36.68,30.52c17.06,26.5,43.3,44.55,73.83,50.9l375.04,79.7Z"/>'
    '</svg>'
)


@register.simple_tag
def money(value, css_class='money'):
    """عرض مبلغ مع رمز الريال السعودي."""
    if value is None or value == '':
        text = '—'
    else:
        text = str(value)
    return format_html(
        '<span class="{}"><span class="money-value">{}</span>{}</span>',
        css_class,
        text,
        mark_safe(SAR_SVG),
    )
