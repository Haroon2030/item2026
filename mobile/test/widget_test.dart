import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:sales_app/main.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('يعرض شاشة الدخول', (WidgetTester tester) async {
    SharedPreferences.setMockInitialValues({});
    await tester.pumpWidget(const SalesApp());
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 50));
    expect(find.text('دخول'), findsOneWidget);
    expect(find.text('مبيعات الرشيد'), findsWidgets);
  });
}
