from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from search.models import UserProfile
from search.validators import contains_sql_injection, sanitize_search_query, ValidationError


class SqlInjectionProtectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='tester',
            password='StrongPassword123!',
        )
        self.client.force_login(self.user)

    def test_detects_classic_sqli_payloads(self):
        self.assertTrue(contains_sql_injection("' OR '1'='1"))
        self.assertTrue(contains_sql_injection('1; DROP TABLE users--'))
        self.assertTrue(contains_sql_injection('x UNION SELECT password FROM auth_user'))
        self.assertFalse(contains_sql_injection('06100'))
        self.assertFalse(contains_sql_injection('202478604'))

    def test_sanitize_search_rejects_sqli(self):
        with self.assertRaises(ValidationError):
            sanitize_search_query("' OR 1=1--")

    def test_sanitize_allows_arabic_item_names(self):
        self.assertEqual(sanitize_search_query('حليب طازج'), 'حليب طازج')
        self.assertEqual(sanitize_search_query('تمر (سكرة)'), 'تمر (سكرة)')

    def test_search_endpoint_blocks_sqli_query(self):
        response = self.client.get(reverse('item_search'), {'q': "' OR 1=1--"})
        self.assertEqual(response.status_code, 403)

    def test_normal_barcode_search_still_allowed(self):
        response = self.client.get(reverse('item_search'), {'q': '06100'})
        self.assertEqual(response.status_code, 200)


class NameSearchTests(TestCase):
    def setUp(self):
        from search.models import ItemBarcode

        self.user = get_user_model().objects.create_user(
            username='namesearcher',
            password='StrongPassword123!',
        )
        self.client.force_login(self.user)
        ItemBarcode.objects.create(
            barcode='1001',
            item_code='A100',
            name='حليب طازج كامل الدسم',
            unit='حبة',
            pack_size='1',
        )
        ItemBarcode.objects.create(
            barcode='1002',
            item_code='A101',
            name='حليب قليل الدسم',
            unit='حبة',
            pack_size='1',
        )
        ItemBarcode.objects.create(
            barcode='2001',
            item_code='B200',
            name='تمر سكرة',
            unit='كيلو',
            pack_size='1',
        )

    def test_name_search_returns_matching_list(self):
        response = self.client.get(reverse('item_search'), {'q': 'حليب'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['match_type'], 'name_list')
        codes = {item['code'] for item in response.context['items']}
        self.assertEqual(codes, {'A100', 'A101'})

    def test_unique_name_resolves_single_item(self):
        with patch('search.views.search_item_details', return_value=[]):
            response = self.client.get(reverse('item_search'), {'q': 'تمر سكرة'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['match_type'], 'name')
        self.assertEqual(response.context['items'][0]['code'], 'B200')


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

    def test_changing_env_password_recovers_bootstrap_login(self):
        with patch.dict(
            'os.environ',
            {'APP_LOGIN_USERNAME': 'admin', 'APP_LOGIN_PASSWORD': 'OldPassword123!'},
        ):
            call_command('ensure_app_user')

        with patch.dict(
            'os.environ',
            {'APP_LOGIN_USERNAME': 'admin', 'APP_LOGIN_PASSWORD': '256400'},
        ):
            call_command('ensure_app_user')

        user = get_user_model().objects.get(username='admin')
        self.assertTrue(user.check_password('256400'))
        self.assertTrue(self.client.login(username='admin', password='256400'))

    def test_ui_created_user_password_survives_bootstrap(self):
        staff = get_user_model().objects.create_user(
            username='0501111111',
            password='StaffPass123!',
            is_staff=True,
        )
        UserProfile.objects.create(
            user=staff, display_name='مشرف', phone='0501111111'
        )

        with patch.dict(
            'os.environ',
            {'APP_LOGIN_USERNAME': 'admin', 'APP_LOGIN_PASSWORD': '256400'},
        ):
            call_command('ensure_app_user')

        staff.refresh_from_db()
        self.assertTrue(staff.check_password('StaffPass123!'))

    def test_login_accepts_phone_number(self):
        user = get_user_model().objects.create_user(
            username='0509999999',
            password='PhoneLogin123!',
            first_name='موظف',
        )
        UserProfile.objects.create(user=user, display_name='موظف', phone='0509999999')

        ok = self.client.login(username='0509999999', password='PhoneLogin123!')
        self.assertTrue(ok)

    def test_login_accepts_display_name(self):
        user = get_user_model().objects.create_user(
            username='0508888888',
            password='NameLogin123!',
            first_name='سارة',
        )
        UserProfile.objects.create(user=user, display_name='سارة', phone='0508888888')

        ok = self.client.login(username='سارة', password='NameLogin123!')
        self.assertTrue(ok)

    @patch.dict(
        'os.environ',
        {'APP_LOGIN_USERNAME': 'admin', 'APP_LOGIN_PASSWORD': '256400'},
    )
    def test_editing_bootstrap_user_keeps_admin_username(self):
        call_command('ensure_app_user')
        admin = get_user_model().objects.get(username='admin')
        self.client.force_login(admin)

        response = self.client.post(
            reverse('user_edit', args=[admin.pk]),
            {
                'name': 'هارون',
                'phone': '0551234567',
                'password': '256400',
            },
        )
        self.assertRedirects(
            response,
            reverse('user_list'),
            fetch_redirect_response=False,
        )

        admin.refresh_from_db()
        self.assertEqual(admin.username, 'admin')
        self.assertEqual(admin.first_name, 'هارون')
        self.assertEqual(admin.profile.phone, '0551234567')
        self.assertTrue(admin.check_password('256400'))

        self.client.logout()
        self.assertTrue(self.client.login(username='admin', password='256400'))
        self.client.logout()
        self.assertTrue(self.client.login(username='هارون', password='256400'))
        self.client.logout()
        self.assertTrue(self.client.login(username='0551234567', password='256400'))


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
                'role_name': 'محاسب',
                'password': 'EmployeePass123!',
            },
        )
        self.assertRedirects(response, reverse('user_list'))
        user = get_user_model().objects.get(username='0503333333')
        self.assertEqual(user.first_name, 'موظف')
        self.assertEqual(user.profile.phone, '0503333333')
        self.assertEqual(user.profile.role_name, 'محاسب')

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
                'role_name': 'مدير فرع',
                'password': '',
            },
        )
        self.assertRedirects(response, reverse('user_list'))
        target.refresh_from_db()
        self.assertEqual(target.first_name, 'محدث')
        self.assertEqual(target.profile.role_name, 'مدير فرع')

        response = self.client.post(reverse('user_delete', args=[target.pk]))
        self.assertRedirects(response, reverse('user_list'))
        self.assertFalse(get_user_model().objects.filter(pk=target.pk).exists())
