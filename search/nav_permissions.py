"""صلاحيات الأقسام الرئيسية والشاشات الفرعية للشريط الجانبي."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# شاشات تفصيلية تُحسب ضمن الشاشة الأم عند الحجب
SCREEN_ALIASES: dict[str, str] = {
    "browse_tr_compare_detail": "browse_tr_compare",
    "browse_pr_compare_detail": "browse_pr_compare",
}

# مسارات بلا قسم (دائماً مسموحة للمُسجّل) أو تُدار بـ is_staff
ALWAYS_ALLOWED: frozenset[str] = frozenset(
    {
        "home",
        "login",
        "logout",
        "sync_barcodes",
    }
)

STAFF_ONLY: frozenset[str] = frozenset(
    {
        "user_list",
        "user_create",
        "user_edit",
        "user_delete",
        "user_activity",
        "user_permissions",
    }
)

# أدوار ترى كل أقسام التطبيق دون إدارة المستخدمين
EXECUTIVE_ROLE_NAMES: frozenset[str] = frozenset(
    {
        "رئيس تنفيذي",
        "مالك",
    }
)

# أدوار مدير قسم: تُمنح قسمها تلقائياً
SECTION_MANAGER_ROLES: dict[str, str] = {
    "مدير مشتريات": "purchases",
    "مدير مبيعات": "sales",
    "مدير تسعيرة": "pricing",
    "مدير مخازن": "inventory",
    "مدير مستودع": "warehouses",
}

ROLE_CHOICES: tuple[tuple[str, str], ...] = (
    ("رئيس تنفيذي", "رئيس تنفيذي — كل الشاشات عدا المستخدمين"),
    ("مالك", "مالك — كل الشاشات عدا المستخدمين"),
    ("مدير مشتريات", "مدير مشتريات — إدارة المشتريات"),
    ("مدير مبيعات", "مدير مبيعات — إدارة المبيعات"),
    ("مدير تسعيرة", "مدير تسعيرة — إدارة التسعيرة"),
    ("مدير مخازن", "مدير مخازن — إدارة المخزون"),
    ("مدير مستودع", "مدير مستودع — إدارة المستودعات"),
)


@dataclass(frozen=True)
class NavScreen:
    key: str
    label: str


@dataclass(frozen=True)
class NavSection:
    key: str
    label: str
    screens: tuple[NavScreen, ...]


NAV_SECTIONS: tuple[NavSection, ...] = (
    NavSection(
        "inventory",
        "إدارة المخزون",
        (
            NavScreen("browse_inventory", "تحليل المخزون"),
            NavScreen("browse_groups", "مخزون المجموعات"),
            NavScreen("browse_unsold", "رصيد بلا مبيعات"),
            NavScreen("browse_inventory_pack_errors", "أخطاء وحدات الأصناف"),
        ),
    ),
    NavSection(
        "purchases",
        "إدارة المشتريات",
        (
            NavScreen("item_search", "بحث الأصناف"),
            NavScreen("browse_purchases", "تحليل المشتريات"),
            NavScreen("browse_purchase_returns", "مردود المشتريات"),
            NavScreen("browse_vendor_turnover", "دوران الموردين"),
            NavScreen("browse_pr_compare", "مقارنات طلب الشراء"),
        ),
    ),
    NavSection(
        "pricing",
        "إدارة التسعيرة",
        (
            NavScreen("browse_low_margin_prices", "حد ربح التسعير"),
            NavScreen("browse_name_barcode_conflicts", "اسم مشابه · باركود مختلف"),
            NavScreen("browse_unpriced_items", "أصناف غير مسعّرة"),
            NavScreen("browse_below_cost_prices", "أقل من التكلفة"),
            NavScreen("browse_tr_compare", "طلب النواقص"),
        ),
    ),
    NavSection(
        "sales",
        "إدارة المبيعات",
        (
            NavScreen("browse_sales", "تحليل المبيعات"),
            NavScreen("sales_search", "البحث عن مبيعات صنف"),
            NavScreen("browse_performance", "تحليل الأداء"),
            NavScreen("browse_sold_no_supply", "بيع بلا توريد"),
        ),
    ),
    NavSection(
        "finance",
        "الإدارة المالية",
        (
            NavScreen("browse_income", "قائمة الدخل"),
            NavScreen("browse_warehouse_expense", "توزيع مصاريف المستودع"),
            NavScreen("browse_trial_balance", "ميزان المراجعة"),
            NavScreen("browse_assets", "الأصول"),
            NavScreen("browse_suppliers", "الموردون"),
        ),
    ),
    NavSection(
        "warehouses",
        "إدارة المستودعات",
        (NavScreen("browse_wh_outgoing", "حركة التحويلات"),),
    ),
)

SECTION_KEYS: tuple[str, ...] = tuple(s.key for s in NAV_SECTIONS)
ALL_SCREEN_KEYS: frozenset[str] = frozenset(
    screen.key for section in NAV_SECTIONS for screen in section.screens
)

_SCREEN_TO_SECTION: dict[str, str] = {
    screen.key: section.key
    for section in NAV_SECTIONS
    for screen in section.screens
}
for alias, parent in SCREEN_ALIASES.items():
    if parent in _SCREEN_TO_SECTION:
        _SCREEN_TO_SECTION[alias] = _SCREEN_TO_SECTION[parent]


def normalize_screen_key(url_name: str | None) -> str:
    key = str(url_name or "").strip()
    return SCREEN_ALIASES.get(key, key)


def section_for_screen(url_name: str | None) -> str | None:
    return _SCREEN_TO_SECTION.get(normalize_screen_key(url_name))


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def user_role_name(user) -> str:
    if user is None or not getattr(user, "is_authenticated", False):
        return ""
    profile = getattr(user, "profile", None)
    return ((profile.role_name if profile else "") or "").strip()


def is_executive_role(user) -> bool:
    """رئيس تنفيذي / مالك: كل الشاشات ما عدا إدارة المستخدمين."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False):
        return False
    return user_role_name(user) in EXECUTIVE_ROLE_NAMES


def is_section_manager_role(user) -> bool:
    return user_role_name(user) in SECTION_MANAGER_ROLES


def preset_sections_for_role(role_name: str) -> list[str] | None:
    """أقسام تُمنح تلقائياً حسب الدور، أو None إن كان الدور يعتمد على الصلاحيات اليدوية."""
    role = str(role_name or "").strip()
    if role in EXECUTIVE_ROLE_NAMES:
        return list(SECTION_KEYS)
    section = SECTION_MANAGER_ROLES.get(role)
    if section and section in SECTION_KEYS:
        return [section]
    return None


def has_full_app_access(user) -> bool:
    """وصول كامل لأقسام التطبيق (بدون بالضرورة إدارة المستخدمين)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False):
        return True
    return is_executive_role(user)


def load_user_permission_row(user) -> Any | None:
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if has_full_app_access(user):
        return None
    cache = getattr(user, "_nav_permission_cache", None)
    if cache is not None:
        return cache[0] if cache else None
    from .models import UserNavPermission

    try:
        row = user.nav_permission
    except UserNavPermission.DoesNotExist:
        row = None
    user._nav_permission_cache = (row,)
    return row


def user_sections(user) -> set[str]:
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    if has_full_app_access(user):
        return set(SECTION_KEYS)
    preset = preset_sections_for_role(user_role_name(user))
    row = load_user_permission_row(user)
    if row is None:
        # بلا سجل: أدوار المدير حسب القسم · غيرهم كل الأقسام حتى الحفظ الأول
        if preset is not None:
            return set(preset)
        return set(SECTION_KEYS)
    saved = {k for k in _as_str_list(row.sections) if k in SECTION_KEYS}
    if not saved and preset is not None:
        return set(preset)
    return saved


def user_blocked_screens(user) -> set[str]:
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    if has_full_app_access(user):
        return set()
    row = load_user_permission_row(user)
    if row is None:
        return set()
    return {
        normalize_screen_key(k)
        for k in _as_str_list(row.blocked_screens)
        if normalize_screen_key(k) in ALL_SCREEN_KEYS
    }


def user_can_access_screen(user, url_name: str | None) -> bool:
    """هل يحق للمستخدم فتح هذه الشاشة (اسم مسار Django)؟"""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    key = str(url_name or "").strip()
    if not key:
        return True
    if key in ALWAYS_ALLOWED:
        return True
    if key in STAFF_ONLY:
        return bool(getattr(user, "is_staff", False))
    if has_full_app_access(user):
        return True

    screen = normalize_screen_key(key)
    section = section_for_screen(key)
    if section is None:
        # مسار غير مصنّف في الكتالوج: اسمح للمُسجّل (APIs مساعدة…)
        return True
    if section not in user_sections(user):
        return False
    if screen in user_blocked_screens(user):
        return False
    return True


def user_can_see_section(user, section_key: str) -> bool:
    if section_key not in user_sections(user):
        return False
    blocked = user_blocked_screens(user)
    section = next((s for s in NAV_SECTIONS if s.key == section_key), None)
    if section is None:
        return False
    return any(screen.key not in blocked for screen in section.screens)


def build_nav_access(user) -> dict[str, Any]:
    """قاموس للقوالب: sections / screens."""
    sections: dict[str, bool] = {}
    screens: dict[str, bool] = {}
    for section in NAV_SECTIONS:
        sections[section.key] = user_can_see_section(user, section.key)
        for screen in section.screens:
            screens[screen.key] = user_can_access_screen(user, screen.key)
    return {
        "is_full": has_full_app_access(user),
        "is_executive": is_executive_role(user),
        "sections": sections,
        "screens": screens,
    }


def permission_form_catalog(
    *,
    selected_sections: Iterable[str] | None = None,
    blocked_screens: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    selected = {str(x).strip() for x in (selected_sections or [])}
    blocked = {normalize_screen_key(x) for x in (blocked_screens or [])}
    out: list[dict[str, Any]] = []
    for section in NAV_SECTIONS:
        out.append(
            {
                "key": section.key,
                "label": section.label,
                "checked": section.key in selected,
                "screens": [
                    {
                        "key": screen.key,
                        "label": screen.label,
                        "blocked": screen.key in blocked,
                    }
                    for screen in section.screens
                ],
            }
        )
    return out


def parse_permission_post(post) -> tuple[list[str], list[str]]:
    """يستخرج الأقسام الممنوحة والشاشات المحجوبة من POST."""
    sections = [
        key
        for key in SECTION_KEYS
        if str(post.get(f"section_{key}") or "").strip() in ("1", "on", "true", "yes")
    ]
    section_set = set(sections)
    blocked: list[str] = []
    for section in NAV_SECTIONS:
        if section.key not in section_set:
            continue
        for screen in section.screens:
            raw = str(post.get(f"block_{screen.key}") or "").strip()
            if raw in ("1", "on", "true", "yes"):
                blocked.append(screen.key)
    return sections, blocked


def ensure_user_nav_permission(user, *, sections: list[str] | None = None) -> Any:
    from .models import UserNavPermission

    defaults = {
        "sections": list(sections) if sections is not None else [],
        "blocked_screens": [],
    }
    row, _ = UserNavPermission.objects.get_or_create(user=user, defaults=defaults)
    if hasattr(user, "_nav_permission_cache"):
        delattr(user, "_nav_permission_cache")
    return row
