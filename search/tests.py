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


class StockCostReportTests(TestCase):
    def setUp(self):
        from search.models import ItemBarcode, ItemGroup

        self.user = get_user_model().objects.create_user(
            username='stockuser',
            password='StrongPassword123!',
        )
        self.client.force_login(self.user)
        ItemGroup.objects.create(g_code='G1', g_name='ألبان')
        ItemGroup.objects.create(g_code='G2', g_name='تمور')
        ItemBarcode.objects.create(
            barcode='1', item_code='A1', name='حليب', unit='كيلو', g_code='G1'
        )
        ItemBarcode.objects.create(
            barcode='2', item_code='A2', name='لبن', unit='كيلو', g_code='G1'
        )
        ItemBarcode.objects.create(
            barcode='3', item_code='B1', name='تمر', unit='كيلو', g_code='G2'
        )

    def test_cached_aggregate_is_instant(self):
        from decimal import Decimal

        from search.api_client import aggregate_group_stock_cost_cached
        from search.models import ItemStockValue

        ItemStockValue.objects.create(
            warehouse='60',
            item_code='A1',
            g_code='G1',
            quantity=Decimal('10'),
            unit_cost=Decimal('5'),
            total_cost=Decimal('50'),
        )
        ItemStockValue.objects.create(
            warehouse='60',
            item_code='A2',
            g_code='G1',
            quantity=Decimal('2'),
            unit_cost=Decimal('10'),
            total_cost=Decimal('20'),
        )
        ItemStockValue.objects.create(
            warehouse='60',
            item_code='B1',
            g_code='G2',
            quantity=Decimal('1'),
            unit_cost=Decimal('100'),
            total_cost=Decimal('100'),
        )
        report = aggregate_group_stock_cost_cached('60')
        self.assertEqual(report['source'], 'cache')
        self.assertEqual(report['grand_total'], 170.0)
        self.assertEqual(len(report['rows']), 2)
        g1 = next(r for r in report['rows'] if r['g_code'] == 'G1')
        self.assertEqual(g1['total_cost'], 70.0)

        filtered = aggregate_group_stock_cost_cached('60', g_code='G1')
        self.assertEqual(filtered['grand_total'], 70.0)
        self.assertEqual(filtered['g_code'], 'G1')

    def test_stock_cost_page_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('stock_cost'))
        self.assertEqual(response.status_code, 302)

    def test_stock_cost_page_loads(self):
        response = self.client.get(reverse('stock_cost'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'تكلفة المخزون')

    @patch('search.stock_views.aggregate_group_stock_cost')
    def test_stock_cost_post_returns_json_report(self, mocked):
        mocked.return_value = {
            'warehouse': '60',
            'g_code': 'G1',
            'g_name': 'ألبان',
            'rows': [
                {
                    'g_code': 'G1',
                    'g_name': 'ألبان',
                    'item_count': 2,
                    'items_valued': 2,
                    'total_cost': 100.5,
                    'total_qty': 10,
                    'total_cost_display': '100.50',
                    'total_qty_display': '10',
                }
            ],
            'grand_total': 100.5,
            'grand_total_display': '100.50',
            'item_total': 3,
            'items_valued': 2,
            'errors': 0,
            'elapsed_sec': 0.01,
            'source': 'cache',
            'cache_updated_display': '2026-07-27 01:00',
        }
        response = self.client.post(
            reverse('stock_cost'),
            {'warehouse': '60', 'g_code': 'G1', 'action': 'view'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['report']['grand_total'], 100.5)
        mocked.assert_called_once_with('60', g_code='G1', refresh=False)

    @patch('search.stock_views.aggregate_group_stock_cost')
    def test_stock_cost_refresh_passes_flag(self, mocked):
        mocked.return_value = {
            'warehouse': '60',
            'g_code': 'G1',
            'g_name': 'ألبان',
            'rows': [],
            'grand_total': 0,
            'grand_total_display': '0.00',
            'item_total': 0,
            'items_valued': 0,
            'errors': 0,
            'elapsed_sec': 2.5,
            'source': 'live',
        }
        response = self.client.post(
            reverse('stock_cost'),
            {'warehouse': '60', 'g_code': 'G1', 'action': 'refresh'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(response.status_code, 200)
        mocked.assert_called_once_with('60', g_code='G1', refresh=True)

    def test_stock_cost_refresh_requires_group(self):
        response = self.client.post(
            reverse('stock_cost'),
            {'warehouse': '60', 'action': 'refresh'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(response.status_code, 502)
        payload = response.json()
        self.assertFalse(payload['ok'])
        self.assertIn('مجموعة', payload['error'])

    def test_stock_cost_page_shows_group_select(self):
        response = self.client.get(reverse('stock_cost'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'كل المجموعات')
        self.assertContains(response, 'عرض من التخزين')
        self.assertContains(response, 'تحديث من النظام')
        self.assertContains(response, 'G1')
        self.assertContains(response, 'ألبان')


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
