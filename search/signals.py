"""تسجيل دخول وخروج المستخدمين في شاشة النشاط."""

from __future__ import annotations

import logging

from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.utils import timezone

from .models import UserActivitySession

logger = logging.getLogger('search')


def _client_ip(request) -> str:
    xff = (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
    ip = xff or (request.META.get('REMOTE_ADDR') or '')
    return ip[:45]


def _user_agent(request) -> str:
    return (request.META.get('HTTP_USER_AGENT') or '')[:300]


def _display_name(user) -> str:
    profile = getattr(user, 'profile', None)
    return (
        (profile.display_name if profile else '')
        or user.first_name
        or user.username
        or ''
    ).strip()


def _phone(user) -> str:
    profile = getattr(user, 'profile', None)
    return ((profile.phone if profile else '') or user.username or '')[:20]


@receiver(user_logged_in)
def record_user_login(sender, request, user, **kwargs):
    try:
        session_key = ''
        if getattr(request, 'session', None) is not None:
            session_key = request.session.session_key or ''
            if not session_key:
                request.session.save()
                session_key = request.session.session_key or ''
            # تحديث إجباري مرة واحدة بعد الدخول (مثل Ctrl+Shift+R)
            request.session['force_hard_refresh'] = '1'
        UserActivitySession.objects.create(
            user=user,
            user_name=_display_name(user)[:150],
            user_phone=_phone(user),
            login_at=timezone.now(),
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            session_key=session_key[:40],
            source='web',
        )
    except Exception:
        logger.exception('failed to record login activity')


@receiver(user_logged_out)
def record_user_logout(sender, request, user, **kwargs):
    if user is None:
        return
    try:
        now = timezone.now()
        qs = UserActivitySession.objects.filter(user=user, logout_at__isnull=True)
        session_key = getattr(request, '_activity_session_key', '') or ''
        row = None
        if session_key:
            row = qs.filter(session_key=session_key).order_by('-login_at').first()
        if row is None:
            row = qs.order_by('-login_at').first()
        if row is None:
            return
        row.logout_at = now
        row.save(update_fields=['logout_at'])
    except Exception:
        logger.exception('failed to record logout activity')
