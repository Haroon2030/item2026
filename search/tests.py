from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from search.models import UserProfile


class AuthenticationTests(TestCase):
    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse('item_search'))

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('item_search')}",
            fetch_redirect_response=False,
        )

    def test_sync_endpoint_requires_login(self):
        response = self.client.post(reverse('sync_barcodes'))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('login')))

    def test_authenticated_user_can_open_search(self):
        user = get_user_model().objects.create_user(
            username='tester',
            password='StrongPassword123!',
        )
        self.client.force_login(user)

        response = self.client.get(reverse('item_search'))

        self.assertEqual(response.status_code, 200)

    @patch.dict(
        'os.environ',
        {
            'APP_LOGIN_USERNAME': 'production-user',
            'APP_LOGIN_PASSWORD': 'StrongProductionPassword123!',
        },
    )
    def test_ensure_app_user_creates_login_account(self):
        call_command('ensure_app_user')

        user = get_user_model().objects.get(username='production-user')
        self.assertTrue(user.check_password('StrongProductionPassword123!'))
        self.assertTrue(user.is_staff)
        self.assertTrue(UserProfile.objects.filter(user=user).exists())


class UserManagementTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username='0501111111',
            password='StrongPassword123!',
            first_name='مشرف',
            is_staff=True,
        )
        UserProfile.objects.create(
            user=self.staff,
            display_name='مشرف',
            phone='0501111111',
        )
        self.client.force_login(self.staff)

    def test_non_staff_cannot_open_users_page(self):
        normal = get_user_model().objects.create_user(
            username='0502222222',
            password='StrongPassword123!',
        )
        self.client.force_login(normal)
        response = self.client.get(reverse('user_list'))
        self.assertEqual(response.status_code, 302)

    def test_staff_can_create_user(self):
        response = self.client.post(
            reverse('user_create'),
            {
                'name': 'موظف',
                'phone': '0503333333',
                'password': 'EmployeePass123!',
            },
        )
        self.assertRedirects(response, reverse('user_list'))
        user = get_user_model().objects.get(username='0503333333')
        self.assertEqual(user.first_name, 'موظف')
        self.assertEqual(user.profile.phone, '0503333333')

    def test_staff_can_edit_and_delete_user(self):
        target = get_user_model().objects.create_user(
            username='0504444444',
            password='StrongPassword123!',
            first_name='قديم',
        )
        UserProfile.objects.create(user=target, display_name='قديم', phone='0504444444')

        response = self.client.post(
            reverse('user_edit', args=[target.pk]),
            {
                'name': 'محدث',
                'phone': '0504444444',
                'password': '',
            },
        )
        self.assertRedirects(response, reverse('user_list'))
        target.refresh_from_db()
        self.assertEqual(target.first_name, 'محدث')

        response = self.client.post(reverse('user_delete', args=[target.pk]))
        self.assertRedirects(response, reverse('user_list'))
        self.assertFalse(get_user_model().objects.filter(pk=target.pk).exists())
