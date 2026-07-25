"""مصادقة بالاسم المستخدم أو رقم الجوال."""

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

from .debug_auth import auth_log, fingerprint


class UsernameOrPhoneBackend(ModelBackend):
    """يقبل الدخول باسم المستخدم أو رقم الهاتف المخزّن في الملف الشخصي."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        if not username or password is None:
            return None

        user_model = get_user_model()
        login_id = str(username).strip()
        submitted = str(password)
        cleaned = submitted.strip()
        user = user_model.objects.filter(username=login_id).first()
        matched_by = 'username' if user is not None else ''
        if user is None:
            user = (
                user_model.objects.filter(profile__phone=login_id)
                .select_related('profile')
                .first()
            )
            if user is not None:
                matched_by = 'phone'
        if user is None:
            # تشغيل hasher حتى لو المستخدم غير موجود (ضد توقيت التخمين)
            user_model().set_password(cleaned)
            # region agent log
            auth_log(
                'A,C',
                'search/auth_backend.py:authenticate',
                'login_user_not_found',
                {
                    'loginFingerprint': fingerprint(login_id),
                    'loginLength': len(login_id),
                    'usersCount': user_model.objects.count(),
                    'submittedPasswordLength': len(submitted),
                    'cleanedPasswordLength': len(cleaned),
                },
            )
            # endregion
            return None
        password_ok = user.check_password(cleaned)
        env_password = os.environ.get('APP_LOGIN_PASSWORD', '')
        env_fingerprint = fingerprint(env_password) if env_password else ''
        submitted_fingerprint = fingerprint(cleaned)
        active_ok = self.user_can_authenticate(user)
        # region agent log
        auth_log(
            'B,C,D,E',
            'search/auth_backend.py:authenticate',
            'login_user_evaluated',
            {
                'loginFingerprint': fingerprint(login_id),
                'userFingerprint': fingerprint(user.username),
                'matchedBy': matched_by,
                'passwordOk': password_ok,
                'activeOk': active_ok,
                'isStaff': user.is_staff,
                'requestSecure': bool(request and request.is_secure()),
                'forwardedProto': (
                    (request.META.get('HTTP_X_FORWARDED_PROTO') or '')[:12]
                    if request
                    else ''
                ),
                'submittedPasswordLength': len(submitted),
                'cleanedPasswordLength': len(cleaned),
                'hadSurroundingWhitespace': submitted != cleaned,
                'matchesEnvPasswordFingerprint': bool(
                    env_fingerprint and submitted_fingerprint == env_fingerprint
                ),
            },
        )
        # endregion
        if password_ok and active_ok:
            return user
        return None
