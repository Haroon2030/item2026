import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:intl/date_symbol_data_local.dart';

import 'screens/login_screen.dart';
import 'screens/shell_screen.dart';
import 'state/app_state.dart';
import 'theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await initializeDateFormatting('ar');
  runApp(const SalesApp());
}

class SalesApp extends StatefulWidget {
  const SalesApp({super.key});

  @override
  State<SalesApp> createState() => _SalesAppState();
}

class _SalesAppState extends State<SalesApp> {
  final AppState _state = AppState();

  @override
  void initState() {
    super.initState();
    _state.addListener(_onChange);
    _state.restore();
  }

  void _onChange() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _state.removeListener(_onChange);
    _state.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'مبيعات الرشيد',
      debugShowCheckedModeBanner: false,
      theme: buildAppTheme(),
      locale: const Locale('ar'),
      supportedLocales: const [Locale('ar'), Locale('en')],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      builder: (context, child) {
        return Directionality(
          textDirection: TextDirection.rtl,
          child: child ?? const SizedBox.shrink(),
        );
      },
      home: _state.booting
          ? const Scaffold(
              body: Center(child: CircularProgressIndicator()),
            )
          : _state.isLoggedIn
              ? ShellScreen(state: _state)
              : LoginScreen(state: _state),
    );
  }
}
