"""مصادقة بالاسم المستخدم أو رقم الجوال."""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class UsernameOrPhoneBackend(ModelBackend):
    """يقبل الدخول باسم المستخدم أو رقم الهاتف المخزّن في الملف الشخصي."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        if not username or password is None:
            return None

        user_model = get_user_model()
        login_id = str(username).strip()
        user = user_model.objects.filter(username=login_id).first()
        if user is None:
            user = (
                user_model.objects.filter(profile__phone=login_id)
                .select_related('profile')
                .first()
            )
        if user is None:
            # تشغيل hasher حتى لو المستخدم غير موجود (ضد توقيت التخمين)
            user_model().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
