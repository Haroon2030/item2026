from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from search.models import UserActivitySession, UserNavPermission, UserProfile
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
        response = self.client.get(reverse('home'))

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('home')}",
            fetch_redirect_response=False,
        )

    def test_home_shows_role_name_after_login(self):
        user = get_user_model().objects.create_user(
            username='0505555555',
            password='StrongPassword123!',
            first_name='سارة',
            is_staff=True,
        )
        UserProfile.objects.create(
            user=user,
            display_name='سارة',
            phone='0505555555',
            role_name='مدير مبيعات',
        )
        self.client.force_login(user)
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'سارة')
        self.assertContains(response, 'مدير مبيعات')
        self.assertContains(response, 'غرفة القرار')

    def test_home_shows_role_cards_for_sales_manager(self):
        user = get_user_model().objects.create_user(
            username='0505666777',
            password='StrongPassword123!',
            first_name='نورة',
            is_staff=False,
        )
        UserProfile.objects.create(
            user=user,
            display_name='نورة',
            phone='0505666777',
            role_name='مدير مبيعات',
        )
        self.client.force_login(user)
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'لإدارة المبيعات')
        self.assertContains(response, 'المبيعات')
        self.assertContains(response, 'تحليل الأداء')
        self.assertNotContains(response, 'قائمة الدخل')
        self.assertNotContains(response, 'حد ربح التسعير')

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
                'role_name': 'مدير تسعيرة',
                'password': 'EmployeePass123!',
            },
        )
        self.assertRedirects(response, reverse('user_list'))
        user = get_user_model().objects.get(username='0503333333')
        self.assertEqual(user.first_name, 'موظف')
        self.assertEqual(user.profile.phone, '0503333333')
        self.assertEqual(user.profile.role_name, 'مدير تسعيرة')

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
                'role_name': 'مدير مشتريات',
                'password': '',
            },
        )
        self.assertRedirects(response, reverse('user_list'))
        target.refresh_from_db()
        self.assertEqual(target.first_name, 'محدث')
        self.assertEqual(target.profile.role_name, 'مدير مشتريات')

        response = self.client.post(reverse('user_delete', args=[target.pk]))
        self.assertRedirects(response, reverse('user_list'))
        self.assertFalse(get_user_model().objects.filter(pk=target.pk).exists())


class UserActivityTests(TestCase):
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

    def test_login_and_logout_are_recorded(self):
        self.assertTrue(self.client.login(username='0501111111', password='StrongPassword123!'))
        row = UserActivitySession.objects.get(user=self.staff)
        self.assertIsNotNone(row.login_at)
        self.assertIsNone(row.logout_at)
        self.assertEqual(row.user_name, 'مشرف')

        response = self.client.post(reverse('logout'))
        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertIsNotNone(row.logout_at)
        self.assertGreaterEqual(row.logout_at, row.login_at)

    def test_non_staff_cannot_open_activity_page(self):
        normal = get_user_model().objects.create_user(
            username='0502222222',
            password='StrongPassword123!',
        )
        self.client.force_login(normal)
        response = self.client.get(reverse('user_activity'))
        self.assertEqual(response.status_code, 302)

    def test_staff_can_open_activity_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('user_activity'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'نشاط المستخدمين')
        self.assertContains(response, 'وقت الدخول')


class VendorTurnoverBindTests(TestCase):
    def test_item_and_vendor_codes_bind_as_text(self):
        from search.oracle_vendor_turnover import _bind_vendor, _icode_in

        _, params = _icode_in(['06100', '2326785'])
        self.assertEqual(params['c0'], '06100')
        self.assertEqual(params['c1'], '2326785')
        self.assertEqual(_bind_vendor('2326785'), '2326785')
        self.assertIsInstance(_bind_vendor('2326785'), str)


class VendorTurnoverCostAdjTests(TestCase):
    def test_prefers_matching_vendor_and_skips_other(self):
        from datetime import datetime

        from search.oracle_vendor_turnover import _index_cost_adj, _pick_cost_adj

        by_item = _index_cost_adj(
            [
                {
                    'i_code': '06100',
                    'v_code': '999',
                    'inc_cost': 1.0,
                    'stk_desc': 'مورد آخر',
                    'doc_date': datetime(2026, 3, 1),
                    'doc_ser': 1,
                },
                {
                    'i_code': '06100',
                    'v_code': '2326785',
                    'inc_cost': 4.48,
                    'stk_desc': 'تسوية تكاليف فاتورة شراء',
                    'doc_date': datetime(2026, 2, 1),
                    'doc_ser': 2,
                },
            ]
        )
        hit = _pick_cost_adj('06100', '2326785', by_item)
        self.assertIsNotNone(hit)
        self.assertTrue(hit['cost_adj'])
        self.assertEqual(hit['cost_adj_label'], 'نعم')
        self.assertEqual(hit['cost_adj_cost'], 4.48)
        self.assertEqual(hit['cost_adj_count'], 1)
        self.assertIsNone(_pick_cost_adj('06100', '111', by_item))

    def test_blank_vendor_attaches_when_no_match(self):
        from search.oracle_vendor_turnover import _index_cost_adj, _pick_cost_adj

        by_item = _index_cost_adj(
            [
                {
                    'i_code': '1001',
                    'v_code': '',
                    'inc_cost': None,
                    'stk_desc': 'خصم 14%',
                    'doc_date': '2026-04-01',
                    'doc_ser': 9,
                }
            ]
        )
        hit = _pick_cost_adj('1001', '2326785', by_item)
        self.assertTrue(hit['cost_adj'])
        self.assertEqual(hit['cost_adj_hint'], 'خصم 14%')
        self.assertEqual(hit['cost_adj_cost_display'], '')


class CostAdjustmentsTests(TestCase):
    @patch('search.oracle_cost_adjustments.oracle_enabled', return_value=True)
    @patch('search.oracle_cost_adjustments._fetch_all')
    def test_shapes_rows_with_account_and_item(self, fetch_all, _enabled):
        from datetime import date, datetime

        from search.oracle_cost_adjustments import fetch_cost_adjustments

        fetch_all.return_value = [
            {
                'DOC_NO': 5501,
                'DOC_SER': 9001,
                'DOC_DATE': datetime(2026, 9, 18, 13, 40),
                'STK_DESC': 'تسوية تكاليف فاتورة شراء',
                'BRN_NO': 6,
                'A_CODE': '21101001',
                'AC_CODE_DTL': '2326785',
                'AC_DTL_TYP': 4,
                'M_V_CODE': None,
                'A_NAME': 'مخزون بضاعة',
                'DTL_NAME': 'مؤسسة الأغذية',
                'I_CODE': '06100',
                'W_CODE': 60,
                'ITM_UNT': 'حبة',
                'P_SIZE': 12,
                'INC_COST': 4.48,
                'WTAVG': 3.9,
                'D_V_CODE': '2326785',
                'I_NAME': 'حليب طازج',
            }
        ]
        report = fetch_cost_adjustments(
            date(2026, 9, 12), date(2026, 9, 18), limit=5
        )
        rows = report['rows']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['item_code'], '06100')
        self.assertEqual(row['item_name'], 'حليب طازج')
        self.assertEqual(row['account_code'], '21101001')
        self.assertEqual(row['account_name'], 'مخزون بضاعة')
        self.assertEqual(row['acct_code'], '21101001')
        self.assertEqual(row['acct_name'], 'مخزون بضاعة')
        self.assertEqual(row['detail_code'], '2326785')
        self.assertEqual(row['detail_name'], 'مؤسسة الأغذية (2326785)')
        self.assertEqual(row['inc_cost'], 4.48)
        self.assertEqual(row['dtl_type'], 4)
        self.assertEqual(row['vendor_code'], '2326785')
        self.assertEqual(row['vendor_name'], 'مؤسسة الأغذية (2326785)')
        self.assertEqual(row['when'], '2026-09-18')
        self.assertEqual(row['wtavg_display'], '3.9')
        self.assertEqual(row['dtl_type_label'], 'مورد')
        self.assertEqual(report['kpis']['move_count'], 1)
        self.assertEqual(report['kpis']['doc_count'], 1)
        self.assertEqual(report['kpis']['account_count'], 1)
        self.assertEqual(report['kpis']['total_docs_display'], '1')
        self.assertEqual(report['kpis']['total_lines_display'], '1')
        self.assertEqual(report['kpis']['distinct_accounts_display'], '1')
        self.assertEqual(len(report['docs']), 1)
        self.assertEqual(report['docs'][0]['doc_no_display'], '5501')
        self.assertEqual(report['docs'][0]['line_count'], 1)
        self.assertEqual(len(report['docs'][0]['lines']), 1)

    @patch('search.oracle_cost_adjustments.oracle_enabled', return_value=True)
    @patch('search.oracle_cost_adjustments._fetch_all')
    def test_groups_lines_by_document(self, fetch_all, _enabled):
        from datetime import date, datetime

        from search.oracle_cost_adjustments import fetch_cost_adjustments

        base = {
            'DOC_NO': 7568,
            'DOC_SER': 99001,
            'DOC_DATE': datetime(2026, 9, 19, 0, 0),
            'STK_DESC': 'تسوية',
            'BRN_NO': 9,
            'A_CODE': '21101001',
            'AC_CODE_DTL': '230302',
            'AC_DTL_TYP': 4,
            'M_V_CODE': None,
            'A_NAME': 'مخزون',
            'DTL_NAME': 'مورد أ',
            'W_CODE': 902,
            'ITM_UNT': 'حبة',
            'P_SIZE': 1,
            'WTAVG': 10,
            'D_V_CODE': None,
        }
        fetch_all.return_value = [
            {**base, 'I_CODE': 'A1', 'I_NAME': 'صنف 1', 'INC_COST': 5},
            {**base, 'I_CODE': 'A2', 'I_NAME': 'صنف 2', 'INC_COST': 7},
            {
                **base,
                'DOC_NO': 7569,
                'DOC_SER': 99002,
                'I_CODE': 'B1',
                'I_NAME': 'صنف ب',
                'INC_COST': 3,
            },
        ]
        report = fetch_cost_adjustments(date(2026, 9, 19), date(2026, 9, 19), limit=20)
        self.assertEqual(len(report['rows']), 3)
        self.assertEqual(len(report['docs']), 2)
        first = report['docs'][0]
        self.assertEqual(first['doc_no_display'], '7568')
        self.assertEqual(first['line_count'], 2)
        self.assertEqual(first['adj_total'], 12.0)
        self.assertEqual([ln['item_code'] for ln in first['lines']], ['A1', 'A2'])

    def test_party_label_strips_nested_parens_and_uses_vendor_code(self):
        from search.oracle_cost_adjustments import _party_label

        self.assertEqual(
            _party_label(
                'مصنع الجميح لتعبئة المرطبات (300056269810003)',
                '231039',
            ),
            'مصنع الجميح لتعبئة المرطبات (231039)',
        )

    @patch('search.oracle_cost_adjustments.oracle_enabled', return_value=True)
    @patch('search.oracle_cost_adjustments._fetch_all')
    def test_adjust_type_hung_filter_and_binds(self, fetch_all, _enabled):
        from datetime import date

        from search.oracle_cost_adjustments import fetch_cost_adjustments

        fetch_all.return_value = []
        fetch_cost_adjustments(
            date(2026, 9, 12),
            date(2026, 9, 18),
            branch_code='6',
            warehouse_code='60',
            group_code='3',
            item_q='حليب',
            account_q='211',
            limit=10,
        )
        sql, params = fetch_all.call_args[0]
        self.assertEqual(params['adj_type'], 2)
        self.assertEqual(params['brn'], 6)
        self.assertEqual(params['wh'], 60)
        self.assertIn('m.ADJUST_TYPE = :adj_type', sql)
        self.assertIn('(m.HUNG IS NULL OR m.HUNG = 0)', sql)
        self.assertIn('d.DOC_SER = m.DOC_SER', sql)
        self.assertIn('INDX_SER_STK_ADJUSTMENT_DET', sql)
        self.assertIn('i.G_CODE = :gcode', sql)
        self.assertEqual(params['iq'], '%حليب%')
        self.assertEqual(params['aq'], '%211%')

    @patch('search.oracle_cost_adjustments.oracle_enabled', return_value=False)
    def test_raises_when_oracle_disabled(self, _enabled):
        from datetime import date

        from search.oracle_cost_adjustments import fetch_cost_adjustments
        from search.oracle_stock import OracleStockError

        with self.assertRaises(OracleStockError):
            fetch_cost_adjustments(date(2026, 9, 12), date(2026, 9, 18))


class CostAdjustmentsViewTests(TestCase):
    def test_browse_cost_adjustments_url_resolves(self):
        self.assertEqual(
            reverse('browse_cost_adjustments'),
            '/purchases/cost-adjustments/',
        )

    def test_browse_cost_adjustments_view_importable(self):
        from search.views import browse_cost_adjustments

        self.assertTrue(callable(browse_cost_adjustments))

    def test_anonymous_user_redirected_to_login(self):
        response = self.client.get(reverse('browse_cost_adjustments'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('login')))

    @patch('search.oracle_stock.oracle_enabled', return_value=False)
    def test_authenticated_user_can_open_page_when_oracle_off(self, _enabled):
        user = get_user_model().objects.create_user(
            username='cadj-user',
            password='StrongPassword123!',
        )
        UserProfile.objects.create(
            user=user,
            display_name='تسعيرة',
            phone='0507000001',
            role_name='مدير تسعيرة',
        )
        self.client.force_login(user)
        response = self.client.get(reverse('browse_cost_adjustments'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'تسوية التكاليف')
        self.assertContains(response, 'أوراكل غير مفعّل')

    def test_denied_without_pricing_nav_access(self):
        user = get_user_model().objects.create_user(
            username='purchases-only',
            password='StrongPassword123!',
        )
        UserProfile.objects.create(
            user=user,
            display_name='مشتريات',
            phone='0507000002',
            role_name='مدير مشتريات',
        )
        UserNavPermission.objects.create(
            user=user,
            sections=['purchases'],
            blocked_screens=[],
        )
        self.client.force_login(user)
        response = self.client.get(reverse('browse_cost_adjustments'))
        self.assertRedirects(
            response,
            reverse('home'),
            fetch_redirect_response=False,
        )


class TransferRequestCompareTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='trcmp',
            password='StrongPassword123!',
        )
        self.client.force_login(self.user)

    def test_list_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('browse_tr_compare'))
        self.assertEqual(response.status_code, 302)

    @patch('search.oracle_stock.oracle_enabled', return_value=False)
    def test_list_renders_when_oracle_off(self, _enabled):
        response = self.client.get(reverse('browse_tr_compare'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'طلب النواقص')
        self.assertContains(response, 'أوراكل غير مفعّل')

    def test_detail_requires_complete_id(self):
        response = self.client.get(reverse('browse_tr_compare_detail'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'معرّف طلب التحويل غير مكتمل')

    def test_pr_summary_for_short_codes(self):
        from search.oracle_tr_compare import _pr_summary_for_codes

        summary = _pr_summary_for_codes(
            ['A1', 'B2', 'C3'],
            {
                'A1': [{'pr_no': '100', 'pr_type': '1', 'pr_ser': '9'}],
                'C3': [
                    {'pr_no': '100', 'pr_type': '1', 'pr_ser': '9'},
                    {'pr_no': '200', 'pr_type': '1', 'pr_ser': '10'},
                ],
            },
        )
        self.assertEqual(summary['short_pr_item_count'], 2)
        self.assertEqual(summary['short_pr_nos'], ['100', '200'])
        self.assertIn('100', summary['short_pr_display'])
        self.assertIn('A1', summary['short_pr_title'])

    @patch('search.oracle_tr_compare.oracle_enabled', return_value=True)
    @patch('search.oracle_tr_compare._fetch_all')
    def test_fetch_recent_pr_by_items(self, fetch_all, _enabled):
        from datetime import date

        from search.oracle_tr_compare import fetch_recent_pr_by_items

        fetch_all.return_value = [
            {
                'ITEM_CODE': '111',
                'PR_TYPE': 1,
                'PR_NO': 4501,
                'PR_SER': 88,
                'PR_WHEN': date(2026, 9, 1),
                'PR_DAY': '2026-09-01',
            },
            {
                'ITEM_CODE': '111',
                'PR_TYPE': 1,
                'PR_NO': 4501,
                'PR_SER': 88,
                'PR_WHEN': date(2026, 9, 1),
                'PR_DAY': '2026-09-01',
            },
            {
                'ITEM_CODE': '222',
                'PR_TYPE': 1,
                'PR_NO': 4502,
                'PR_SER': 89,
                'PR_WHEN': date(2026, 9, 2),
                'PR_DAY': '2026-09-02',
            },
        ]
        by_item = fetch_recent_pr_by_items(['111', '222', '333'], day=date(2026, 9, 3))
        self.assertEqual(len(by_item['111']), 1)
        self.assertEqual(by_item['111'][0]['pr_no'], '4501')
        self.assertEqual(by_item['222'][0]['pr_no'], '4502')
        self.assertNotIn('333', by_item)


class MainWarehouseForBranchTests(TestCase):
    @patch('search.oracle_tr_compare.oracle_enabled', return_value=True)
    @patch('search.oracle_tr_compare._fetch_all')
    def test_sky_picks_warehouse_60(self, fetch_all, _enabled):
        from search.oracle_tr_compare import fetch_main_warehouse_for_branch

        fetch_all.return_value = [
            {'W_CODE': 15, 'W_NAME': 'مخزن 15', 'MAIN_WCODE': 0},
            {'W_CODE': 60, 'W_NAME': 'مخزن 6 (سكاي مول)', 'MAIN_WCODE': 0},
            {'W_CODE': 62, 'W_NAME': 'مخزن 62', 'MAIN_WCODE': 0},
        ]
        hit = fetch_main_warehouse_for_branch('6')
        self.assertEqual(hit['code'], '60')
        self.assertEqual(hit['name'], 'مخزن 6 (سكاي مول)')

    @patch('search.oracle_tr_compare.oracle_enabled', return_value=True)
    @patch('search.oracle_tr_compare._fetch_all')
    def test_prefers_times_100_when_times_10_missing(self, fetch_all, _enabled):
        from search.oracle_tr_compare import fetch_main_warehouse_for_branch

        fetch_all.return_value = [
            {'W_CODE': 900, 'W_NAME': 'مخزن 9', 'MAIN_WCODE': 0},
            {'W_CODE': 91, 'W_NAME': 'مخزن 91', 'MAIN_WCODE': 0},
        ]
        hit = fetch_main_warehouse_for_branch('9')
        self.assertEqual(hit['code'], '900')


class PosSalesTableTests(TestCase):
    @patch(
        'search.sales_dashboard._pos_store_branches',
        return_value={'6': 'سكاي مول'},
    )
    def test_pos_table_keeps_active_store(self, _stores):
        from datetime import date

        from search.sales_dashboard import _assemble_sales_branches_dashboard

        payload = _assemble_sales_branches_dashboard(
            [
                {
                    'branch_code': '6',
                    'branch_name': 'سكاي مول',
                    'invoice_count': 12,
                    'return_count': 0,
                    'return_total': 0,
                    'sales_total': 1500,
                    'avg_basket': 125,
                }
            ],
            [],
            [],
            date(2026, 8, 1),
            date(2026, 8, 16),
        )
        branches = payload['pos']['branches']
        self.assertEqual([row['branch_code'] for row in branches], ['6'])
        self.assertFalse(any(row.get('no_sales') for row in branches))

    @patch(
        'search.sales_dashboard._pos_store_branches',
        return_value={'6': 'سكاي مول', '18': 'فرع حائل', '7': 'فرع الدمام'},
    )
    def test_listed_pos_stores_without_sales_are_red_zeros(self, _stores):
        from datetime import date

        from search.sales_dashboard import _assemble_sales_branches_dashboard

        payload = _assemble_sales_branches_dashboard(
            [
                {
                    'branch_code': '6',
                    'branch_name': 'سكاي مول',
                    'invoice_count': 12,
                    'return_count': 0,
                    'return_total': 0,
                    'sales_total': 1500,
                    'avg_basket': 125,
                }
            ],
            [],
            [],
            date(2026, 8, 1),
            date(2026, 8, 16),
        )
        by_code = {row['branch_code']: row for row in payload['pos']['branches']}
        self.assertEqual(set(by_code), {'6', '18', '7'})
        self.assertFalse(by_code['6']['no_sales'])
        self.assertTrue(by_code['18']['no_sales'])
        self.assertTrue(by_code['7']['no_sales'])
        self.assertEqual(by_code['18']['sales_total_display'], '0.00')

    def test_pos_store_name_tokens(self):
        from search.sales_dashboard import _is_pos_store_name

        self.assertTrue(_is_pos_store_name('فرع الدمام'))
        self.assertTrue(_is_pos_store_name('سكاي مول'))
        self.assertTrue(_is_pos_store_name('فرع الربوة'))
        self.assertTrue(_is_pos_store_name('فرع الوااحة'))
        self.assertTrue(_is_pos_store_name('خميس مشيط'))
        self.assertFalse(_is_pos_store_name('الإدارة العامة'))
        self.assertFalse(_is_pos_store_name('فرع الثلاجة'))

    @patch(
        'search.sales_dashboard._pos_store_branches',
        return_value={'7': 'فرع الدمام', '6': 'سكاي مول'},
    )
    def test_dammam_sales_merge_float_branch_code(self, _stores):
        """مبيعات الدمام لا تُفصل عن صف الأصفار عند BRN=7.0 من أوراكل."""
        from datetime import date

        from search.sales_dashboard import _assemble_sales_branches_dashboard

        payload = _assemble_sales_branches_dashboard(
            [
                {
                    'branch_code': '7.0',
                    'branch_name': '7.0',
                    'invoice_count': 40,
                    'return_count': 1,
                    'return_total': 50,
                    'sales_total': 9000,
                    'avg_basket': 225,
                }
            ],
            [],
            [],
            date(2026, 8, 1),
            date(2026, 8, 16),
        )
        by_code = {row['branch_code']: row for row in payload['pos']['branches']}
        self.assertIn('7', by_code)
        self.assertNotIn('7.0', by_code)
        self.assertEqual(by_code['7']['branch_name'], 'فرع الدمام')
        self.assertFalse(by_code['7']['no_sales'])
        self.assertEqual(by_code['7']['sales_total'], 9000.0)


class VendorQueryFilterTests(TestCase):
    def test_matches_name_and_code(self):
        from search.oracle_vendor_turnover import apply_vendor_query

        report = {
            'rows': [
                {
                    'vendor_code': '1203',
                    'vendor_name': 'مؤسسة صالح',
                    'decision_key': 'settle',
                    'recv_qty': 10,
                    'sold_qty': 8,
                    'due_amt': 100,
                },
                {
                    'vendor_code': '88',
                    'vendor_name': 'شركة الأغذية',
                    'decision_key': 'hold',
                    'recv_qty': 4,
                    'sold_qty': 1,
                    'due_amt': 20,
                },
            ],
            'kpis': {},
        }
        by_name = apply_vendor_query(report, 'صالح')
        self.assertEqual([r['vendor_code'] for r in by_name['rows']], ['1203'])
        by_code = apply_vendor_query(report, '88')
        self.assertEqual([r['vendor_code'] for r in by_code['rows']], ['88'])
        self.assertEqual(apply_vendor_query(report, 'لا يوجد')['rows'], [])


class IncomeTrendCompareTests(TestCase):
    def test_prior_period_bounds_same_length(self):
        from datetime import date

        from search.oracle_income import _prior_period_bounds

        prior_from, prior_to = _prior_period_bounds(date(2026, 4, 1), date(2026, 6, 30))
        self.assertEqual(prior_to, date(2026, 3, 31))
        self.assertEqual(prior_from, date(2025, 12, 31))
        self.assertEqual((prior_to - prior_from).days, (date(2026, 6, 30) - date(2026, 4, 1)).days)

    def test_monthly_trend_fills_months_and_bars(self):
        from datetime import date

        from search.oracle_income import _build_monthly_trend

        rows = [
            {
                'YM': date(2026, 1, 1),
                'ROOT': '3',
                'NORM_AMT': 1000,
                'MV_DR': 0,
                'MV_CR': 1000,
            },
            {
                'YM': date(2026, 1, 1),
                'ROOT': '5',
                'NORM_AMT': 200,
                'MV_DR': 200,
                'MV_CR': 0,
            },
            {
                'YM': date(2026, 2, 1),
                'ROOT': '3',
                'NORM_AMT': 500,
                'MV_DR': 0,
                'MV_CR': 500,
            },
        ]
        trend = _build_monthly_trend(date(2026, 1, 1), date(2026, 3, 15), rows)
        self.assertEqual(trend['month_count'], 3)
        self.assertTrue(trend['has_data'])
        by_ym = {m['ym']: m for m in trend['months']}
        self.assertEqual(by_ym['2026-01-01']['revenue'], 1000.0)
        self.assertEqual(by_ym['2026-01-01']['expense'], 200.0)
        self.assertEqual(by_ym['2026-01-01']['net'], 800.0)
        self.assertEqual(by_ym['2026-03-01']['revenue'], 0.0)
        self.assertEqual(by_ym['2026-01-01']['net_pct'], '100.0')
        self.assertGreater(float(by_ym['2026-01-01']['net_pct']), float(by_ym['2026-02-01']['net_pct']))
        self.assertEqual(by_ym['2026-01-01']['net_compact'], '800')
        self.assertNotIn(',', by_ym['2026-01-01']['net_pct'])
        self.assertIn('م', _build_monthly_trend(
            date(2026, 1, 1),
            date(2026, 1, 31),
            [{'YM': date(2026, 1, 1), 'ROOT': '3', 'NORM_AMT': 2_500_000, 'MV_DR': 0, 'MV_CR': 2_500_000}],
        )['months'][0]['revenue_compact'])

    def test_period_compare_delta_kinds(self):
        from datetime import date

        from search.oracle_income import _build_period_compare

        current = {
            'revenue': 1200,
            'cogs': 400,
            'expense': 250,
            'net': 550,
            'net_title': 'صافي الربح',
        }
        prior = {
            'revenue': 1000,
            'cogs': 300,
            'expense': 300,
            'net': 400,
            'net_title': 'صافي الربح',
        }
        compare = _build_period_compare(
            current,
            prior,
            date(2025, 1, 1),
            date(2025, 9, 23),
        )
        by_key = {r['key']: r for r in compare['rows']}
        self.assertEqual(by_key['revenue']['delta_kind'], 'up')
        self.assertEqual(by_key['expense']['delta_kind'], 'up')  # مصروف أقل = تحسّن
        self.assertEqual(by_key['cogs']['delta_kind'], 'down')  # تكلفة أعلى = تراجع
        self.assertEqual(by_key['net']['delta_kind'], 'up')
        self.assertEqual(by_key['revenue']['delta_pct_display'], '+20.0%')
        self.assertTrue(compare['has_data'])


class IncomeExecutiveAlertsTests(TestCase):
    def test_losing_branches_and_concentration(self):
        from search.oracle_income import _build_executive_alerts

        alerts = _build_executive_alerts(
            kpis={'expense': 1000},
            by_branch_profit=[
                {'branch_name': 'أ', 'net': 800, 'net_kind': 'profit'},
                {'branch_name': 'ب', 'net': 200, 'net_kind': 'profit'},
                {'branch_name': 'ج', 'net': -150, 'net_kind': 'loss'},
            ],
            top_accounts=[],
            monthly_trend={'months': []},
        )
        keys = [a['key'] for a in alerts['alerts']]
        self.assertIn('losing_branches', keys)
        self.assertIn('concentration', keys)
        self.assertTrue(alerts['has_alerts'])

    def test_expense_spike_and_margin_pressure(self):
        from search.oracle_income import _build_executive_alerts

        alerts = _build_executive_alerts(
            kpis={'expense': 1000},
            by_branch_profit=[],
            top_accounts=[
                {'account_name': 'رواتب', 'impact': 400},
            ],
            monthly_trend={
                'months': [
                    {'label_short': 'يناير', 'revenue': 1000, 'net': 120},
                    {'label_short': 'فبراير', 'revenue': 1000, 'net': 110},
                    {'label_short': 'مارس', 'revenue': 1000, 'net': 40},
                ],
            },
        )
        keys = [a['key'] for a in alerts['alerts']]
        self.assertIn('expense_spike', keys)
        self.assertIn('margin_pressure', keys)

    def test_stable_when_no_exceptions(self):
        from search.oracle_income import _build_executive_alerts

        alerts = _build_executive_alerts(
            kpis={'expense': 1000},
            by_branch_profit=[
                {'branch_name': 'أ', 'net': 220, 'net_kind': 'profit'},
                {'branch_name': 'ب', 'net': 210, 'net_kind': 'profit'},
                {'branch_name': 'ج', 'net': 200, 'net_kind': 'profit'},
                {'branch_name': 'د', 'net': 190, 'net_kind': 'profit'},
                {'branch_name': 'هـ', 'net': 180, 'net_kind': 'profit'},
            ],
            top_accounts=[{'account_name': 'كهرباء', 'impact': 100}],
            monthly_trend={
                'months': [
                    {'label_short': 'يناير', 'revenue': 1000, 'net': 100},
                    {'label_short': 'فبراير', 'revenue': 1000, 'net': 105},
                    {'label_short': 'مارس', 'revenue': 1000, 'net': 102},
                ],
            },
        )
        self.assertFalse(alerts['has_alerts'])
        self.assertEqual(alerts['summary'], 'لا تنبيهات')


class IncomeExpenseMixDonutTests(TestCase):
    def test_expense_mix_builds_slices_and_high_decision(self):
        from search.oracle_income import _build_expense_mix_donut

        by_account = [
            {'kind': 'expense', 'account_code': '1', 'account_name': 'رواتب', 'mv_dr': 700, 'mv_cr': 0},
            {'kind': 'expense', 'account_code': '2', 'account_name': 'إيجار', 'mv_dr': 150, 'mv_cr': 0},
            {'kind': 'expense', 'account_code': '3', 'account_name': 'كهرباء', 'mv_dr': 80, 'mv_cr': 0},
            {'kind': 'expense', 'account_code': '4', 'account_name': 'أخرى1', 'mv_dr': 40, 'mv_cr': 0},
            {'kind': 'expense', 'account_code': '5', 'account_name': 'أخرى2', 'mv_dr': 30, 'mv_cr': 0},
        ]
        mix = _build_expense_mix_donut(by_account, {'expense': 1000}, head=3)
        self.assertTrue(mix['has_data'])
        self.assertGreaterEqual(len(mix['slices']), 3)
        self.assertTrue(any(s.get('path_d') for s in mix['slices']))
        self.assertEqual(mix['decision_tone'], 'high')
        self.assertIn('رواتب', mix['decision'])
        self.assertEqual(mix['center']['label'], 'المصروفات')

    def test_expense_mix_other_bucket(self):
        from search.oracle_income import _build_expense_mix_donut

        by_account = [
            {'kind': 'expense', 'account_code': str(i), 'account_name': f'ب{i}', 'mv_dr': 100, 'mv_cr': 0}
            for i in range(1, 8)
        ]
        mix = _build_expense_mix_donut(by_account, {'expense': 700}, head=5)
        keys = [s['key'] for s in mix['slices']]
        self.assertIn('other', keys)
        other = next(s for s in mix['slices'] if s['key'] == 'other')
        self.assertEqual(other['amount'], 200.0)


class AssetsExecutiveReportTests(TestCase):
    def test_depr_ratio_and_structure(self):
        from search.oracle_assets import _build_asset_structure, _share_display

        structure = _build_asset_structure(1000.0, 400.0, 600.0)
        self.assertTrue(structure['has_data'])
        self.assertEqual(structure['center']['label'], 'التكلفة')
        keys = [s['key'] for s in structure['slices']]
        self.assertEqual(keys, ['depr', 'book'])
        by_key = {s['key']: s for s in structure['slices']}
        self.assertEqual(by_key['depr']['share_pct'], 40.0)
        self.assertEqual(by_key['book']['share_pct'], 60.0)
        self.assertTrue(any(s.get('path_d') for s in structure['slices']))
        self.assertEqual(_share_display(62.5), '62%')
        self.assertNotIn(',', _share_display(86.3))
        self.assertEqual(_share_display(8.3), '8.3%')

    def test_group_mix_other_bucket_and_dominance(self):
        from search.oracle_assets import _build_group_mix

        rows = [
            {'group_code': 'G1', 'group_name': 'مباني', 'cost': 500, 'depr': 100, 'book_value': 400},
            {'group_code': 'G2', 'group_name': 'سيارات', 'cost': 120, 'depr': 40, 'book_value': 80},
            {'group_code': 'G3', 'group_name': 'أثاث', 'cost': 80, 'depr': 20, 'book_value': 60},
            {'group_code': 'G4', 'group_name': 'أجهزة', 'cost': 50, 'depr': 10, 'book_value': 40},
            {'group_code': 'G5', 'group_name': 'أدوات', 'cost': 40, 'depr': 5, 'book_value': 35},
            {'group_code': 'G6', 'group_name': 'أخرى1', 'cost': 30, 'depr': 5, 'book_value': 25},
            {'group_code': 'G7', 'group_name': 'أخرى2', 'cost': 20, 'depr': 2, 'book_value': 18},
        ]
        mix = _build_group_mix(rows, head=5)
        self.assertTrue(mix['has_data'])
        keys = [s['key'] for s in mix['slices']]
        self.assertIn('other', keys)
        other = next(s for s in mix['slices'] if s['key'] == 'other')
        self.assertEqual(other['amount'], 50.0)
        self.assertEqual(mix['decision_tone'], 'high')
        self.assertIn('مباني', mix['decision'])
        self.assertTrue(any(s.get('path_d') for s in mix['slices']))

    def test_executive_alerts_concentration_and_worn(self):
        from search.oracle_assets import _build_executive_alerts, _build_group_mix

        branch_rows = [
            {'branch_name': 'فرع أ', 'cost_total': 700},
            {'branch_name': 'فرع ب', 'cost_total': 200},
            {'branch_name': 'فرع ج', 'cost_total': 100},
        ]
        rows = [
            {
                'group_code': 'G1',
                'group_name': 'مباني',
                'cost': 100,
                'depr': 97,
                'book_value': 3,
            },
            {
                'group_code': 'G1',
                'group_name': 'مباني',
                'cost': 50,
                'depr': 48,
                'book_value': 2,
            },
            {
                'group_code': 'G2',
                'group_name': 'سيارات',
                'cost': 850,
                'depr': 100,
                'book_value': 750,
            },
        ]
        group_mix = _build_group_mix(rows, head=5)
        alerts = _build_executive_alerts(
            branch_rows=branch_rows,
            group_mix=group_mix,
            rows=rows,
            cost_total=1000.0,
        )
        keys = [a['key'] for a in alerts['alerts']]
        self.assertIn('branch_concentration', keys)
        self.assertIn('fully_depreciated', keys)
        self.assertIn('group_dominance', keys)
        self.assertTrue(alerts['has_alerts'])
        worn = next(a for a in alerts['alerts'] if a['key'] == 'fully_depreciated')
        self.assertIn('2 أصل', worn['metric'])

    def test_executive_alerts_stable_when_balanced(self):
        from search.oracle_assets import _build_executive_alerts, _build_group_mix

        branch_rows = [
            {'branch_name': f'ف{i}', 'cost_total': 200}
            for i in range(1, 6)
        ]
        rows = [
            {
                'group_code': f'G{i}',
                'group_name': f'مجموعة {i}',
                'cost': 200,
                'depr': 40,
                'book_value': 160,
            }
            for i in range(1, 6)
        ]
        group_mix = _build_group_mix(rows, head=5)
        alerts = _build_executive_alerts(
            branch_rows=branch_rows,
            group_mix=group_mix,
            rows=rows,
            cost_total=1000.0,
        )
        self.assertFalse(alerts['has_alerts'])
        self.assertEqual(alerts['summary'], 'لا تنبيهات')

    @patch('search.oracle_assets.cache')
    @patch('search.oracle_assets._branch_names', return_value={'1': 'فرع 1', '2': 'فرع 2'})
    @patch('search.oracle_assets._fetch_all')
    @patch('search.oracle_assets.oracle_enabled', return_value=True)
    def test_build_assets_report_depr_ratio_kpi(self, _enabled, mock_fetch, _names, mock_cache):
        from search.oracle_assets import build_assets_report

        mock_cache.get.return_value = None
        mock_fetch.return_value = [
            {
                'BRN_NO': 1,
                'AS_CODE': 'A1',
                'AS_NAME': 'أصل 1',
                'GRP_CODE': 'G1',
                'GRP_NAME': 'مباني',
                'PRCH_DATE': None,
                'COST': 800,
                'DEPR': 200,
                'BV': 600,
            },
            {
                'BRN_NO': 2,
                'AS_CODE': 'A2',
                'AS_NAME': 'أصل 2',
                'GRP_CODE': 'G2',
                'GRP_NAME': 'سيارات',
                'PRCH_DATE': None,
                'COST': 200,
                'DEPR': 50,
                'BV': 150,
            },
        ]
        report = build_assets_report()
        self.assertEqual(report['kpis']['depr_ratio'], 25.0)
        self.assertEqual(report['kpis']['depr_ratio_display'], '25%')
        self.assertTrue(report['structure']['has_data'])
        self.assertTrue(report['group_mix']['has_data'])
        self.assertEqual(report['top_assets']['count'], 2)
        self.assertIn('executive_alerts', report)
        self.assertEqual(report['branch_rows'][0]['bv_bar_pct'], '100.0')


class PurchaseInvoiceMovementTests(TestCase):
    """حركة فواتير الشراء: توزيع FIFO لصافي البيع على فواتير الصنف في فرعه."""

    @staticmethod
    def _day(n):
        from datetime import date, timedelta

        return date(2026, 9, 1) + timedelta(days=n - 1)

    def _purchase(self, ser, day, qty, cost, *, item='100', brn=6, vendor='V1', name='حليب'):
        return {
            'BILL_SER': ser,
            'BILL_NO': ser + 1000,
            'BILL_DATE': self._day(day),
            'V_CODE': vendor,
            'V_NAME': f'مورد {vendor}',
            'BRN_NO': brn,
            'I_CODE': item,
            'I_NAME': name,
            'G_CODE': 5,
            'QTY': qty,
            'COST': cost,
        }

    def test_older_invoice_is_consumed_first_and_cumulative_carries_over(self):
        from search.oracle_stock_turnover import allocate_fifo

        d = self._day
        result = allocate_fifo(
            [('A', d(1), 10), ('B', d(3), 5)],
            {d(1): 4, d(2): 4, d(3): 5, d(4): 1},
        )
        a, b = result['lines']['A'], result['lines']['B']
        self.assertEqual(a['sold'], 10)
        self.assertEqual(a['sold_out'], d(3))
        self.assertEqual(b['sold'], 4)
        self.assertIsNone(b['sold_out'])
        self.assertEqual((b['cum_qty'], b['cum_sold']), (15, 14))
        self.assertEqual(result['prior'], 0)
        self.assertEqual(result['last_sale'], d(4))

    def test_sales_before_arrival_or_beyond_received_count_as_prior_stock(self):
        from search.oracle_stock_turnover import allocate_fifo

        d = self._day
        early = allocate_fifo([('A', d(5), 10)], {d(3): 6, d(6): 3})
        self.assertEqual(early['lines']['A']['sold'], 3)
        self.assertEqual(early['prior'], 6)

        overflow = allocate_fifo([('A', d(1), 5), ('B', d(2), 5)], {d(1): 8})
        self.assertEqual(overflow['lines']['A']['sold'], 5)
        self.assertEqual(overflow['lines']['B']['sold'], 0)
        self.assertEqual(overflow['prior'], 3)

    def test_pos_return_puts_quantity_back_and_reopens_invoice(self):
        from search.oracle_stock_turnover import allocate_fifo

        d = self._day
        result = allocate_fifo([('A', d(1), 5)], {d(1): 5, d(2): -2})
        self.assertEqual(result['lines']['A']['sold'], 3)
        self.assertIsNone(result['lines']['A']['sold_out'])

    def test_lines_carry_status_value_and_queue(self):
        from search.oracle_stock_turnover import compute_invoice_lines

        d = self._day
        lines = compute_invoice_lines(
            [
                self._purchase(1, 1, 10, 50),
                self._purchase(2, 2, 12, 48),
                self._purchase(3, 1, 8, 40, item='200', name='أرز'),
            ],
            {'100|6': {d(1): 10, d(10): 1}},
            today=d(40),
            branch_names={'6': 'فرع 6'},
        )
        by_key = {line['key']: line for line in lines}
        first, second, dead = by_key['1|100'], by_key['2|100'], by_key['3|200']
        self.assertEqual(first['status'], 'fast_out')
        self.assertEqual(first['sellout_days'], 0)
        self.assertEqual(second['sold'], 1)
        self.assertEqual(second['remaining'], 11)
        self.assertEqual(second['remaining_value'], 44.0)
        self.assertEqual(second['status'], 'stagnant')
        self.assertEqual(second['branch_name'], 'فرع 6')
        self.assertEqual(dead['sold'], 0)
        self.assertEqual(dead['status'], 'stagnant')

    def test_projection_includes_invoices_ahead_in_the_queue(self):
        from search.oracle_stock_turnover import compute_invoice_lines

        d = self._day
        daily = {d(n): 2 for n in range(1, 11)}
        lines = compute_invoice_lines(
            [self._purchase(1, 1, 30, 30), self._purchase(2, 5, 30, 30)],
            {'100|6': daily},
            today=d(10),
        )
        first, second = sorted(lines, key=lambda line: line['bill_ser'])
        self.assertEqual(first['sold'], 20)
        self.assertEqual(first['clear_days'], 5.0)
        self.assertTrue(second['queued'])
        self.assertEqual(second['clear_days'], 20.0)
        self.assertEqual(first['status'], 'fast')
        self.assertEqual(second['status'], 'mid')

    def test_vendor_filter_runs_after_allocation(self):
        from search.oracle_stock_turnover import compute_invoice_lines, filter_lines

        d = self._day
        lines = compute_invoice_lines(
            [self._purchase(1, 1, 5, 5, vendor='A'), self._purchase(2, 2, 5, 5, vendor='B')],
            {'100|6': {d(3): 6}},
            today=d(4),
        )
        only_b = filter_lines(lines, vendor_code='B')
        self.assertEqual([line['sold'] for line in only_b], [1])

    def test_invoice_rollup_takes_status_of_largest_open_value(self):
        from search.oracle_stock_turnover import build_invoice_rows, compute_invoice_lines

        d = self._day
        lines = compute_invoice_lines(
            [
                self._purchase(1, 1, 4, 4, item='100'),
                self._purchase(1, 1, 10, 100, item='200'),
                self._purchase(2, 1, 3, 3, item='300'),
            ],
            {'100|6': {d(2): 4}, '300|6': {d(3): 3}},
            today=d(40),
        )
        rows = {row['bill_ser']: row for row in build_invoice_rows(lines)}
        self.assertEqual(rows['1']['status'], 'stagnant')
        self.assertEqual((rows['1']['line_count'], rows['1']['open_count']), (2, 1))
        self.assertEqual(rows['1']['remaining_value'], 100.0)
        self.assertEqual(rows['2']['status'], 'fast_out')
        self.assertEqual(rows['2']['sellout_days'], 2)

    def test_period_window_depends_on_branch(self):
        from search.oracle_stock import OracleStockError
        from search.oracle_stock_turnover import validate_period

        d = self._day
        with self.assertRaises(OracleStockError):
            validate_period(d(1), d(5), today=d(60), branch_code='')
        self.assertEqual(validate_period(d(1), d(90), today=d(60), branch_code='6'), d(60))
        with self.assertRaises(OracleStockError):
            validate_period(d(1), d(5), today=d(200), branch_code='6')

    def test_sql_leads_by_date_and_joins_pairs_without_exists(self):
        from search.oracle_stock_turnover import _daily_sales_sql, _purchase_lines_sql

        with patch('search.oracle_stock_turnover._schema', return_value='S'), patch(
            'search.oracle_stock_turnover._pos_owner', return_value='P'
        ):
            purchase_sql, purchase_bind = _purchase_lines_sql(branch_code='6', group_code='')
            sale_sql, sale_bind = _daily_sales_sql(kind='pos', branch_code='6', group_code='')
            return_sql, _bind = _daily_sales_sql(kind='pos_return', branch_code='', group_code='')
            bill_sql, _bind = _daily_sales_sql(kind='bill', branch_code='', group_code='5')
            bill_rt_sql, _bind = _daily_sales_sql(kind='bill_return', branch_code='', group_code='')
        self.assertIn('INDX_SER_PI_BILL_DTL', purchase_sql)
        self.assertIn('FREE_QTY', purchase_sql)
        self.assertIsInstance(purchase_bind['brn'], int)
        self.assertIsInstance(sale_bind['brn'], int)
        for sql in (sale_sql, return_sql, bill_sql, bill_rt_sql):
            self.assertIn('p.BRN_NO = m.BRN_NO', sql)
            self.assertIn('>= p.FIRST_DAY', sql)
            self.assertNotIn('EXISTS', sql)
            self.assertNotIn('TO_CHAR(d.I_CODE', sql)
            self.assertNotIn('NVL(m.BILL_SER', sql)
        self.assertIn('IAS_POS_RT_BILL_MST', return_sql)
        self.assertIn('d.BILL_SER = m.BILL_SER', bill_sql)
        self.assertIn('INDX_SER_BILL_DTL', bill_sql)
        self.assertIn('CNCL_FLG', bill_sql)
        self.assertIn('pi.G_CODE = :gcode', bill_sql)
        self.assertIn('d.RT_BILL_SER = m.RT_BILL_SER', bill_rt_sql)

    def test_day_slices_cover_window_without_overlap(self):
        from search.oracle_stock_turnover import day_slices

        d = self._day
        slices = day_slices(d(1), d(31), 4)
        self.assertEqual(len(slices), 4)
        self.assertEqual(slices[0][0], d(1))
        self.assertEqual(slices[-1][1], d(31))
        for (_a, end), (start, _b) in zip(slices, slices[1:]):
            self.assertEqual(end, start)
        self.assertEqual(sum((b - a).days for a, b in slices), 30)
        self.assertEqual(day_slices(d(1), d(3), 4), [(d(1), d(2)), (d(2), d(3))])
        self.assertEqual(day_slices(d(1), d(2), 1), [(d(1), d(2))])

    def test_selling_branch_check_reads_headers_only(self):
        from search.oracle_stock_turnover import _selling_branches_sql

        with patch('search.oracle_stock_turnover._schema', return_value='IAS'), patch(
            'search.oracle_stock_turnover._pos_owner', return_value='YSPOS1'
        ):
            pos_sql = _selling_branches_sql('pos')
            bill_sql = _selling_branches_sql('bill')
        self.assertIn('INDEX(m POSBILLMST_BILLDATEUSRBRN)', pos_sql)
        self.assertNotIn('DTL', pos_sql)
        self.assertNotIn('HUNG', pos_sql)
        self.assertIn('IAS.IAS_BILL_MST', bill_sql)
        self.assertIn('CNCL_FLG', bill_sql)
        self.assertNotIn('DTL', bill_sql)

    def test_invoice_drilldown_matches_invoice_only_not_item_codes(self):
        from search.oracle_stock_turnover import compute_invoice_lines, filter_lines

        d = self._day
        lines = compute_invoice_lines(
            [
                self._purchase(6506, 1, 10, 10, item='121301'),
                self._purchase(37171, 2, 10, 10, item='10007025622445765061'),
            ],
            {},
            today=d(5),
        )
        by_query = filter_lines(lines, item_query='6506')
        self.assertEqual([line['bill_ser'] for line in by_query], ['37171'])
        by_bill = filter_lines(lines, bill_ser='6506')
        self.assertEqual([line['item_code'] for line in by_bill], ['121301'])

    def test_branch_without_any_sales_is_not_called_slow_or_stagnant(self):
        from search.oracle_stock_turnover import compute_invoice_lines

        d = self._day
        lines = compute_invoice_lines(
            [self._purchase(1, 1, 10, 10, brn=2), self._purchase(2, 1, 10, 10, brn=6)],
            {},
            today=d(40),
            selling_branches={'6'},
        )
        by_branch = {line['branch_code']: line['status'] for line in lines}
        self.assertEqual(by_branch, {'2': 'no_branch_sales', '6': 'stagnant'})

    @patch('search.oracle_stock_turnover._attach_item_barcodes')
    @patch('search.oracle_stock_turnover.oracle_enabled', return_value=True)
    def test_report_pages_on_server_and_ignores_unknown_vendor(self, _enabled, _barcodes):
        from search.oracle_stock_turnover import build_stock_turnover_report, compute_invoice_lines

        d = self._day
        lines = compute_invoice_lines(
            [self._purchase(n, 1, 5, 10, item=str(500 + n)) for n in range(1, 121)],
            {},
            today=d(10),
        )
        with patch('search.oracle_stock_turnover.load_invoice_lines', return_value=lines):
            report = build_stock_turnover_report(
                d(1), d(10), view='line', page=3, vendor_code='nope', today=d(10)
            )
            waiting = build_stock_turnover_report(
                d(1), d(10), view='invoice', status='stagnant', today=d(10)
            )
        self.assertEqual((report['total'], report['page_count']), (120, 3))
        self.assertEqual((len(report['rows']), report['page_start']), (20, 100))
        self.assertEqual(report['vendor'], '')
        self.assertEqual(report['kpis']['invoice_count'], 120)
        self.assertEqual(report['status_counts'][0]['code'], 'waiting')
        self.assertEqual(waiting['total'], 0)

    def test_anonymous_user_is_redirected(self):
        response = self.client.get(reverse('browse_stock_turnover'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('login')))

    @patch('search.oracle_stock.oracle_enabled', return_value=False)
    def test_page_opens_when_oracle_off(self, _enabled):
        user = get_user_model().objects.create_user(username='inv-user', password='StrongPassword123!')
        UserProfile.objects.create(user=user, display_name='مخازن', phone='0507000011', role_name='مدير مخازن')
        self.client.force_login(user)
        response = self.client.get(reverse('browse_stock_turnover'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'حركة فواتير الشراء')
        self.assertContains(response, 'أوراكل غير مفعّل')

    def test_denied_without_inventory_nav_access(self):
        user = get_user_model().objects.create_user(username='pur-only', password='StrongPassword123!')
        UserProfile.objects.create(user=user, display_name='مشتريات', phone='0507000012', role_name='مدير مشتريات')
        UserNavPermission.objects.create(user=user, sections=['purchases'], blocked_screens=[])
        self.client.force_login(user)
        response = self.client.get(reverse('browse_stock_turnover'))
        self.assertRedirects(response, reverse('home'), fetch_redirect_response=False)


_LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'income-tests'}}


@override_settings(CACHES=_LOCMEM_CACHE)
class IncomeUnpostedCardTests(TestCase):
    """بطاقة «مبيعات لم تُرحّل»: رؤوس POS بـ POSTED=0 — تُعرض مستقلة ولا تغيّر أرقام الدفتر."""

    LEDGER_BASE = [
        {'A_CODE': '31001', 'A_NAME': 'مبيعات', 'DR': 0, 'OPEN_NET': 0,
         'MV_DR': 0, 'MV_CR': 1000, 'NORM_AMT': 1000, 'LINE_COUNT': 5},
        {'A_CODE': '41001', 'A_NAME': 'تكلفة مبيعات', 'DR': 1, 'OPEN_NET': 0,
         'MV_DR': 800, 'MV_CR': 0, 'NORM_AMT': 800, 'LINE_COUNT': 5},
    ]

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    @staticmethod
    def _unposted():
        from datetime import date

        return {
            'by_branch': {
                '7': {'amount': 500.0, 'cost': 420.0, 'bills': 12, 'returns': 1, 'oldest': date(2026, 9, 26)},
                '9': {'amount': 300.0, 'cost': 240.0, 'bills': 4, 'returns': 0, 'oldest': date(2026, 9, 27)},
            },
            'missing_cost_items': 0,
        }

    def _build(self, *, unposted=None, unposted_error=None, **kwargs):
        from datetime import date

        from search.oracle_income import build_income_statement

        fetch_kwargs = {'side_effect': unposted_error} if unposted_error else {'return_value': unposted or self._unposted()}
        with patch('search.oracle_income._fetch_income_base_rows', return_value=list(self.LEDGER_BASE)), \
                patch('search.oracle_income.fetch_income_branch_profits', return_value=[]), \
                patch('search.oracle_income._fetch_income_kind_rows', return_value=[]), \
                patch('search.oracle_income.fetch_cash_box_checks', return_value={'all_ok': True, 'summary': 'ok', 'chart': []}), \
                patch('search.oracle_income._branch_names', return_value={'7': 'فرع 7', '9': 'فرع 9'}), \
                patch('search.oracle_income.fetch_unposted_pos_sales', **fetch_kwargs) as fetch:
            statement = build_income_statement(date(2026, 9, 1), date(2026, 9, 28), **kwargs)
        return statement, fetch

    def test_unposted_sql_leads_by_date_index_and_filters_unposted_only(self):
        from search.oracle_income import _unposted_pos_return_sql, _unposted_pos_sql

        with patch('search.oracle_income._pos_owner', return_value='YSPOS1'):
            sales = _unposted_pos_sql()
            returns = _unposted_pos_return_sql()
        self.assertIn('INDEX(m POSBILLMST_BILLDATEUSRBRN)', sales)
        self.assertIn('m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl', sales)
        self.assertIn('d.BILL_NO = h.BILL_NO AND d.BRN_NO = h.BRN_NO', sales)
        self.assertIn('d.RT_BILL_NO = h.RT_BILL_NO AND d.BRN_NO = h.BRN_NO', returns)
        for sql in (sales, returns):
            self.assertIn('(m.POSTED IS NULL OR m.POSTED = 0)', sql)
            self.assertIn('(m.HUNG IS NULL OR m.HUNG = 0)', sql)
            self.assertNotIn('NVL(m.POSTED', sql)
            self.assertNotIn('TO_CHAR(d.BILL_NO', sql)
            self.assertIn('MATERIALIZE', sql)

    def test_slices_cover_period_contiguously_and_only_old_months_are_cacheable(self):
        from datetime import date, timedelta

        from search.oracle_income import _unposted_slices

        today = date(2026, 9, 28)
        slices = _unposted_slices(date(2026, 1, 1), today, today)
        self.assertEqual(slices[0][0], date(2026, 1, 1))
        self.assertEqual(slices[-1][1], today)
        for (_s, end, _c), (nxt, _e, _c2) in zip(slices, slices[1:]):
            self.assertEqual(nxt, end + timedelta(days=1))
        recent_start = today - timedelta(days=45)
        for start, end, cacheable in slices:
            self.assertEqual(cacheable, end < recent_start)
            if cacheable:
                self.assertEqual((start.year, start.month), (end.year, end.month))

    def test_aggregate_nets_returns_and_costs_by_warehouse_item(self):
        from datetime import datetime

        from search.oracle_income import _aggregate_unposted

        sales = [
            {'K': 'H', 'BRN': 7, 'D': datetime(2026, 9, 27), 'N': 10, 'AMT': 1000.0},
            {'K': 'H', 'BRN': 7, 'D': datetime(2026, 9, 25), 'N': 2, 'AMT': 200.0},
            {'K': 'L', 'BRN': 7, 'D': datetime(2026, 9, 1), 'ITEM_CODE': '100', 'W_CODE': '1', 'STOCK_QTY': 10},
            {'K': 'L', 'BRN': 7, 'D': datetime(2026, 9, 1), 'ITEM_CODE': '200', 'W_CODE': '1', 'STOCK_QTY': 5},
            {'K': 'L', 'BRN': 7, 'D': datetime(2026, 9, 1), 'ITEM_CODE': '999', 'W_CODE': '1', 'STOCK_QTY': 3},
        ]
        returns = [
            {'K': 'H', 'BRN': 7, 'D': datetime(2026, 9, 27), 'N': 1, 'AMT': 50.0},
            {'K': 'L', 'BRN': 7, 'D': datetime(2026, 9, 1), 'ITEM_CODE': '100', 'W_CODE': '1', 'STOCK_QTY': 1},
        ]
        costs = {('100', '1'): 40.0, ('200', '1'): 60.0}
        result = _aggregate_unposted(sales, returns, lambda i, w: costs.get((i, w), 0.0))
        b = result['by_branch']['7']
        self.assertEqual(b['amount'], 1150.0)
        self.assertEqual(b['cost'], 10 * 40 + 5 * 60 - 1 * 40)
        self.assertEqual((b['bills'], b['returns']), (12, 1))
        self.assertEqual(b['oldest'].isoformat(), '2026-09-25')
        self.assertEqual(result['missing_cost_items'], 1)

    def test_card_is_separate_and_ledger_figures_stay_unchanged(self):
        statement, fetch = self._build()
        fetch.assert_called_once()
        self.assertEqual(statement['kpis']['revenue'], 1000)
        self.assertEqual(statement['kpis']['cogs'], 800)
        u = statement['unposted']
        self.assertTrue(u['has_data'])
        self.assertEqual((u['amount'], u['cost'], u['gross']), (800, 660, 140))
        self.assertEqual((u['bills'], u['returns'], u['branch_count']), (16, 1, 2))
        self.assertEqual(u['oldest'], '2026-09-26')
        self.assertEqual(u['top_branches'][0]['branch_code'], '7')

    def test_branch_filter_limits_card_to_that_branch(self):
        statement, _fetch = self._build(branch_code='9')
        self.assertEqual(statement['unposted']['amount'], 300)
        self.assertEqual(statement['unposted']['branch_count'], 1)

    def test_cost_center_filter_skips_card(self):
        statement, fetch = self._build(cc_code='12')
        fetch.assert_not_called()
        self.assertFalse(statement['unposted']['has_data'])

    def test_unposted_failure_keeps_statement_with_error(self):
        from search.oracle_stock import OracleStockError

        statement, _fetch = self._build(unposted_error=OracleStockError('timeout'))
        self.assertEqual(statement['kpis']['revenue'], 1000)
        self.assertIn('تعذّرت', statement['unposted']['error'])

    def test_anonymous_user_is_redirected(self):
        response = self.client.get(reverse('browse_income'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('login')))

    def test_page_renders_unposted_card(self):
        from contextlib import nullcontext

        statement, _fetch = self._build()
        user = get_user_model().objects.create_user(username='fin-user', password='StrongPassword123!')
        UserProfile.objects.create(user=user, display_name='مالية', phone='0507000021', role_name='مدير مالي')
        self.client.force_login(user)
        with patch('search.oracle_stock.oracle_enabled', return_value=True), \
                patch('search.oracle_stock.oracle_session', return_value=nullcontext()), \
                patch('search.oracle_income.fetch_income_branches', return_value=[]), \
                patch('search.oracle_income.fetch_income_cost_centers', return_value=[]), \
                patch('search.oracle_income.build_income_statement', return_value=statement):
            response = self.client.get(reverse('browse_income'), {'date_from': '2026-09-01', 'date_to': '2026-09-28'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'مبيعات لم تُرحّل')
        self.assertContains(response, '800.00')
        self.assertContains(response, '1,000.00')
        self.assertNotContains(response, 'فروع لم تُرحّل')
        self.assertContains(response, 'أقدم يوم <span class="mono" dir="ltr">2026-09-26</span>', html=False)
