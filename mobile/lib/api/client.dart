import 'dart:convert';

import 'package:http/http.dart' as http;

import 'models.dart';

class SalesApi {
  SalesApi({String? baseUrl, http.Client? client})
      : baseUrl = (baseUrl ?? const String.fromEnvironment(
              'API_BASE',
              defaultValue: 'https://item.alrsheed.net',
            ))
            .replaceAll(RegExp(r'/$'), ''),
        _client = client ?? http.Client();

  final String baseUrl;
  final http.Client _client;
  String? token;

  Uri _uri(String path, [Map<String, String>? query]) {
    return Uri.parse('$baseUrl$path').replace(queryParameters: query);
  }

  Map<String, String> get _headers {
    final headers = <String, String>{
      'Accept': 'application/json',
      'Content-Type': 'application/json',
    };
    if (token != null && token!.isNotEmpty) {
      headers['Authorization'] = 'Bearer $token';
    }
    return headers;
  }

  Future<Map<String, dynamic>> _decode(http.Response response) async {
    Map<String, dynamic> body = {};
    if (response.body.isNotEmpty) {
      try {
        final decoded = jsonDecode(response.body);
        if (decoded is Map<String, dynamic>) {
          body = decoded;
        }
      } on FormatException {
        throw ApiException(
          'تعذّر الاتصال بالخادم.',
          statusCode: response.statusCode,
        );
      }
    }
    if (response.statusCode == 401) {
      throw ApiException(
        '${body['error'] ?? 'انتهت الجلسة. سجّل الدخول مجدداً.'}',
        statusCode: 401,
      );
    }
    if (response.statusCode >= 400 || body['ok'] == false) {
      throw ApiException(
        '${body['error'] ?? 'تعذّر تنفيذ الطلب.'}',
        statusCode: response.statusCode,
      );
    }
    return body;
  }

  Future<http.Response> _send(Future<http.Response> request) {
    return request.timeout(const Duration(seconds: 45));
  }

  Future<({String token, AppUser user})> login({
    required String username,
    required String password,
  }) async {
    final response = await _send(
      _client.post(
        _uri('/api/mobile/login/'),
        headers: _headers,
        body: jsonEncode({'username': username, 'password': password}),
      ),
    );
    final body = await _decode(response);
    final userJson = (body['user'] as Map?)?.cast<String, dynamic>() ?? {};
    return (
      token: '${body['token'] ?? ''}',
      user: AppUser.fromJson(userJson),
    );
  }

  Future<void> logout() async {
    try {
      await _send(_client.post(_uri('/api/mobile/logout/'), headers: _headers));
    } catch (_) {
      // تجاهل فشل الخروج الشبكي — نمسح الرمز محلياً في كل الأحوال
    }
  }

  Future<AppUser> me() async {
    final body = await _decode(
      await _send(_client.get(_uri('/api/mobile/me/'), headers: _headers)),
    );
    return AppUser.fromJson(
      (body['user'] as Map?)?.cast<String, dynamic>() ?? {},
    );
  }

  Future<({List<FilterOption> branches, List<FilterOption> groups})>
      filters() async {
    final body = await _decode(
      await _send(_client.get(_uri('/api/mobile/filters/'), headers: _headers)),
    );
    return (
      branches: [
        for (final row in (body['branches'] as List? ?? []))
          if (row is Map) FilterOption.fromJson(row.cast<String, dynamic>()),
      ],
      groups: [
        for (final row in (body['groups'] as List? ?? []))
          if (row is Map) FilterOption.fromJson(row.cast<String, dynamic>()),
      ],
    );
  }

  Map<String, String> _salesQuery({
    required String dateFrom,
    required String dateTo,
    String branch = '',
    String group = '',
  }) {
    return {
      'date_from': dateFrom,
      'date_to': dateTo,
      if (branch.isNotEmpty) 'branch': branch,
      if (group.isNotEmpty) 'group': group,
    };
  }

  Future<DailySales> dailySales({
    required String dateFrom,
    required String dateTo,
    String branch = '',
    String group = '',
  }) async {
    final body = await _decode(
      await _send(
        _client.get(
          _uri(
            '/api/mobile/sales/daily/',
            _salesQuery(
              dateFrom: dateFrom,
              dateTo: dateTo,
              branch: branch,
              group: group,
            ),
          ),
          headers: _headers,
        ),
      ),
    );
    final daily = (body['daily'] as Map?)?.cast<String, dynamic>() ?? {};
    daily['date_from'] = body['daily'] == null
        ? dateFrom
        : (daily['date_from'] ?? dateFrom);
    daily['date_to'] = daily['date_to'] ?? dateTo;
    return DailySales.fromJson(daily);
  }

  Future<GroupSales> groupSales({
    required String dateFrom,
    required String dateTo,
    String branch = '',
    String group = '',
  }) async {
    final body = await _decode(
      await _send(
        _client.get(
          _uri(
            '/api/mobile/sales/groups/',
            _salesQuery(
              dateFrom: dateFrom,
              dateTo: dateTo,
              branch: branch,
              group: group,
            ),
          ),
          headers: _headers,
        ),
      ),
    );
    return GroupSales.fromJson(
      (body['groups'] as Map?)?.cast<String, dynamic>() ?? {},
    );
  }
}
