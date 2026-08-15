import 'package:flutter/material.dart';

class AppColors {
  static const ink = Color(0xFF0F172A);
  static const muted = Color(0xFF5B6B7C);
  static const brand = Color(0xFF0E4A6E);
  static const brandDark = Color(0xFF0A3550);
  static const brandMid = Color(0xFF156089);
  static const brandSoft = Color(0xFFE8F2F7);
  static const accent = Color(0xFF0F7A6B);
  static const accentSoft = Color(0xFFE6F5F2);
  static const danger = Color(0xFFB42318);
  static const dangerSoft = Color(0xFFFEE4E2);
  static const canvas = Color(0xFFF3F6F9);
  static const card = Color(0xFFFFFFFF);
}

ThemeData buildAppTheme() {
  const scheme = ColorScheme.light(
    primary: AppColors.brand,
    onPrimary: Colors.white,
    secondary: AppColors.accent,
    onSecondary: Colors.white,
    surface: AppColors.card,
    onSurface: AppColors.ink,
    error: AppColors.danger,
  );
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: AppColors.canvas,
    fontFamily: 'SF Pro Text',
    appBarTheme: const AppBarTheme(
      backgroundColor: AppColors.brandDark,
      foregroundColor: Colors.white,
      elevation: 0,
      centerTitle: true,
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: Colors.white,
      border: OutlineInputBorder(borderRadius: BorderRadius.circular(14)),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: Color(0xFFD5E0EA)),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: AppColors.brand, width: 1.4),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: AppColors.brand,
        foregroundColor: Colors.white,
        minimumSize: const Size.fromHeight(52),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        textStyle: const TextStyle(fontWeight: FontWeight.w700, fontSize: 16),
      ),
    ),
  );
}
