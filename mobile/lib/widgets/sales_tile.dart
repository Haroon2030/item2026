import 'dart:ui';

import 'package:flutter/material.dart';

import '../theme.dart';

class SalesTile extends StatelessWidget {
  const SalesTile({
    super.key,
    required this.title,
    required this.subtitle,
    required this.amount,
    required this.sharePct,
    required this.shareDisplay,
    this.dimmed = false,
  });

  final String title;
  final String subtitle;
  final String amount;
  final double sharePct;
  final String shareDisplay;
  final bool dimmed;

  @override
  Widget build(BuildContext context) {
    final progress = (sharePct / 100).clamp(0.0, 1.0);
    return Opacity(
      opacity: dimmed ? 0.55 : 1,
      child: Container(
        margin: const EdgeInsets.only(bottom: 10),
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: const Color(0xFFE2EAF1)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    title,
                    style: const TextStyle(
                      fontWeight: FontWeight.w800,
                      fontSize: 15,
                      color: AppColors.ink,
                    ),
                  ),
                ),
                Text(
                  amount,
                  style: const TextStyle(
                    fontWeight: FontWeight.w800,
                    fontSize: 16,
                    color: AppColors.brandDark,
                    fontFeatures: [FontFeature.tabularFigures()],
                  ),
                ),
              ],
            ),
            const SizedBox(height: 6),
            Text(
              subtitle,
              style: const TextStyle(color: AppColors.muted, fontSize: 12),
            ),
            const SizedBox(height: 10),
            ClipRRect(
              borderRadius: BorderRadius.circular(99),
              child: LinearProgressIndicator(
                value: progress,
                minHeight: 6,
                backgroundColor: const Color(0xFFE8EEF3),
                color: AppColors.accent,
              ),
            ),
            const SizedBox(height: 6),
            Text(
              shareDisplay,
              style: const TextStyle(
                color: AppColors.accent,
                fontWeight: FontWeight.w700,
                fontSize: 12,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
