import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/client.dart';
import '../api/models.dart';

class AppState extends ChangeNotifier {
  AppState({SalesApi? api}) : api = api ?? SalesApi();

  final SalesApi api;

  AppUser? user;
  DateTime dateFrom = DateTime.now();
  DateTime dateTo = DateTime.now();
  String branch = '';
  String group = '';
  List<FilterOption> branches = const [];
  List<FilterOption> groups = const [];
  DailySales? daily;
  GroupSales? groupSales;
  bool booting = true;
  bool loadingDaily = false;
  bool loadingGroups = false;
  String? dailyError;
  String? groupsError;

  bool get isLoggedIn => (api.token ?? '').isNotEmpty;

  String get dateFromText => _iso(dateFrom);
  String get dateToText => _iso(dateTo);

  String _iso(DateTime d) =>
      '${d.year.toString().padLeft(4, '0')}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';

  Future<void> restore() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString('mobile_token') ?? '';
    if (saved.isEmpty) {
      booting = false;
      notifyListeners();
      return;
    }
    api.token = saved;
    try {
      user = await api.me();
      await loadFilters();
      await refreshAll();
    } on ApiException catch (e) {
      if (e.needsLogin) {
        await logout(remote: false);
      } else {
        dailyError = e.message;
      }
    } catch (e) {
      dailyError = 'تعذّر الاتصال بالخادم.';
    }
    booting = false;
    notifyListeners();
  }

  Future<void> login(String username, String password) async {
    final result = await api.login(username: username, password: password);
    api.token = result.token;
    user = result.user;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('mobile_token', result.token);
    await loadFilters();
    await refreshAll();
    notifyListeners();
  }

  Future<void> logout({bool remote = true}) async {
    if (remote && isLoggedIn) {
      await api.logout();
    }
    api.token = null;
    user = null;
    daily = null;
    groupSales = null;
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('mobile_token');
    notifyListeners();
  }

  Future<void> loadFilters() async {
    try {
      final result = await api.filters();
      branches = result.branches;
      groups = result.groups;
    } catch (_) {
      // الفلاتر اختيارية — تبقى القوائم فارغة
    }
  }

  Future<void> setRange(DateTime from, DateTime to) async {
    dateFrom = from;
    dateTo = to;
    notifyListeners();
    await refreshAll();
  }

  Future<void> setToday() => setRange(DateTime.now(), DateTime.now());

  Future<void> setBranch(String code) async {
    branch = code;
    notifyListeners();
    await refreshAll();
  }

  Future<void> setGroup(String code) async {
    group = code;
    notifyListeners();
    await refreshAll();
  }

  Future<void> refreshAll() async {
    await Future.wait([refreshDaily(), refreshGroups()]);
  }

  Future<void> refreshDaily() async {
    if (!isLoggedIn) return;
    loadingDaily = true;
    dailyError = null;
    notifyListeners();
    try {
      daily = await api.dailySales(
        dateFrom: dateFromText,
        dateTo: dateToText,
        branch: branch,
        group: group,
      );
    } on ApiException catch (e) {
      if (e.needsLogin) {
        await logout(remote: false);
        return;
      }
      dailyError = e.message;
    } catch (_) {
      dailyError = 'تعذّر جلب المبيعات اليومية.';
    }
    loadingDaily = false;
    notifyListeners();
  }

  Future<void> refreshGroups() async {
    if (!isLoggedIn) return;
    loadingGroups = true;
    groupsError = null;
    notifyListeners();
    try {
      groupSales = await api.groupSales(
        dateFrom: dateFromText,
        dateTo: dateToText,
        branch: branch,
        group: group,
      );
    } on ApiException catch (e) {
      if (e.needsLogin) {
        await logout(remote: false);
        return;
      }
      groupsError = e.message;
    } catch (_) {
      groupsError = 'تعذّر جلب مبيعات المجموعات.';
    }
    loadingGroups = false;
    notifyListeners();
  }
}
