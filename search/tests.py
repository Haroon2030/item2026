from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
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


class StockTurnoverMathTests(TestCase):
    def test_cover_days_uses_current_stock_over_daily_sales(self):
        from search.oracle_stock_turnover import cover_days, delay_days, turnover_times

        self.assertEqual(cover_days(300, 100, 30), 90)
        self.assertEqual(delay_days(90), 45)
        self.assertEqual(delay_days(10), 0)
        self.assertIsNone(delay_days(None))
        self.assertIsNone(cover_days(300, 0, 30))
        self.assertIsNone(cover_days(0, 100, 30))
        self.assertEqual(turnover_times(100, 50), 2)

    def test_dead_stock_is_on_hand_with_no_net_sales(self):
        from search.oracle_stock_turnover import assemble_turnover_rows

        report = assemble_turnover_rows(
            [{'code': '10', 'name': 'ألبان', 'qty': 40, 'value': 800, 'item_count': 3}],
            {},
            period_days_count=30,
        )
        row = report['rows'][0]
        self.assertEqual(row['band'], 'dead')
        self.assertEqual(row['band_label'], 'راكد')
        self.assertEqual(row['cover_display'], '—')
        self.assertEqual(row['delay_display'], 'راكد')
        self.assertIsNone(row['delay_days'])
        self.assertEqual(row['turnover_display'], '0')
        self.assertEqual(
            assemble_turnover_rows(
                [{'code': '9', 'name': 'نادر', 'qty': 100000, 'value': 1}],
                {'9': 1},
                period_days_count=30,
            )['rows'][0]['turnover_display'],
            '<0.01',
        )

    def test_stagnant_filter_keeps_stored_or_depleted_rows(self):
        from search.oracle_stock_turnover import (
            STATUS_OPTIONS,
            assemble_turnover_rows,
            rows_for_status,
        )

        self.assertEqual(
            list(STATUS_OPTIONS),
            [("", "الكل"), ("dead", "مخزن"), ("out", "نفذ")],
        )
        report = assemble_turnover_rows(
            [
                {'code': '1', 'name': 'سريع', 'qty': 10, 'value': 100},
                {'code': '2', 'name': 'راكد', 'qty': 80, 'value': 900},
            ],
            {'1': 30, '3': 12},
            period_days_count=30,
            names={'3': 'نافد'},
        )
        self.assertEqual(
            [row['code'] for row in rows_for_status(report['rows'], '')],
            ['2', '1', '3'],
        )
        self.assertEqual(
            [row['code'] for row in rows_for_status(report['rows'], 'dead')],
            ['2'],
        )
        self.assertEqual(
            [row['code'] for row in rows_for_status(report['rows'], 'out')],
            ['3'],
        )
        self.assertEqual(rows_for_status(report['rows'], 'unknown'), report['rows'])

    def test_stockout_and_fast_cover_sort_after_dead(self):
        from search.oracle_stock_turnover import assemble_turnover_rows

        report = assemble_turnover_rows(
            [
                {'code': '1', 'name': 'سريع', 'qty': 10, 'value': 100},
                {'code': '2', 'name': 'راكد', 'qty': 80, 'value': 900},
            ],
            {'1': 30, '3': 12},
            period_days_count=30,
            names={'3': 'نافد'},
        )
        self.assertEqual([row['code'] for row in report['rows']], ['2', '1', '3'])
        self.assertEqual(report['rows'][1]['band'], 'fast')
        self.assertEqual(report['rows'][1]['cover_days'], 10)
        self.assertEqual(report['rows'][1]['delay_days'], 0)
        self.assertEqual(report['rows'][1]['delay_display'], '0')
        self.assertEqual(report['rows'][2]['band'], 'out')
        self.assertEqual(report['rows'][2]['turnover_display'], '—')

    def test_kpis_use_full_set_when_table_is_capped(self):
        from search.oracle_stock_turnover import (
            _kpis_from_rows,
            assemble_turnover_rows,
        )

        stock = [
            {'code': str(i), 'name': f'صنف {i}', 'qty': 100, 'value': 10}
            for i in range(5)
        ]
        sales = {str(i): 1 for i in range(5)}
        shown = assemble_turnover_rows(
            stock, sales, period_days_count=30, row_cap=2
        )
        full = assemble_turnover_rows(stock, sales, period_days_count=30)
        kpis = _kpis_from_rows(full['rows'], period_days_count=30)
        self.assertEqual(shown['shown_count'], 2)
        self.assertEqual(shown['total_count'], 5)
        self.assertTrue(shown['truncated'])
        self.assertEqual(kpis['stock_qty'], 500)
        self.assertEqual(kpis['sold_qty'], 5)
        self.assertEqual(kpis['cover_days'], 3000)
        self.assertEqual(kpis['delay_days'], 2955)

    def test_barcode_prefers_stock_unit_longest_single_code(self):
        from search.oracle_stock_turnover import _choose_barcode

        self.assertEqual(
            _choose_barcode(
                [
                    (1, 0, '12400'),
                    (1, 0, '000000444225'),
                    (12, 0, '999'),
                ]
            ),
            '000000444225',
        )
        self.assertEqual(
            _choose_barcode([(1, 0, '12400'), (1, 1, '555')]),
            '555',
        )

    def test_purchase_qty_does_not_change_cover_or_create_balance(self):
        from search.oracle_stock_turnover import assemble_turnover_rows

        report = assemble_turnover_rows(
            [{'code': '46', 'name': 'تغليف', 'qty': 100, 'value': 50}],
            {'46': 20},
            purchase_qty={'46': 8, '99': 500},
            period_days_count=10,
        )
        row = report['rows'][0]
        self.assertEqual(row['purchased_qty'], 8)
        self.assertEqual(row['sold_qty'], 20)
        self.assertEqual(row['stock_qty'], 100)
        self.assertEqual(row['cover_days'], 50)
        self.assertEqual(len(report['rows']), 1)

    def test_item_branch_rows_keep_code_barcode_slot_and_branch(self):
        from search.oracle_stock_turnover import assemble_turnover_rows

        report = assemble_turnover_rows(
            [
                {
                    'code': '100|2',
                    'name': 'حليب',
                    'qty': 10,
                    'value': 40,
                },
                {
                    'code': '100|6',
                    'name': 'حليب',
                    'qty': 4,
                    'value': 16,
                },
            ],
            {'100|2': 2},
            period_days_count=30,
            extras={
                '100|2': {
                    'item_code': '100',
                    'branch_code': '2',
                    'branch_name': 'فرع الربوة',
                },
                '100|6': {
                    'item_code': '100',
                    'branch_code': '6',
                    'branch_name': 'فرع البلاستيك',
                },
            },
        )
        self.assertEqual(
            [(row['item_code'], row['branch_name'], row['sold_qty']) for row in report['rows']],
            [('100', 'فرع البلاستيك', 0), ('100', 'فرع الربوة', 2)],
        )
