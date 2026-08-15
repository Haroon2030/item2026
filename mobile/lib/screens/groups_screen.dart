import 'package:flutter/material.dart';

import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/kpi_card.dart';
import '../widgets/sales_tile.dart';

class GroupsScreen extends StatelessWidget {
  const GroupsScreen({super.key, required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    final data = state.groupSales;
    if (state.loadingGroups && data == null) {
      return const Center(child: CircularProgressIndicator());
    }
    if (state.groupsError != null && data == null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(state.groupsError!, textAlign: TextAlign.center),
              const SizedBox(height: 12),
              FilledButton(
                onPressed: state.refreshGroups,
                child: const Text('إعادة المحاولة'),
              ),
            ],
          ),
        ),
      );
    }
    if (data == null) {
      return const Center(child: Text('لا توجد بيانات لعرضها.'));
    }

    return RefreshIndicator(
      onRefresh: state.refreshGroups,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 28),
        children: [
          if (data.warning.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: Text(
                data.warning,
                style: const TextStyle(color: AppColors.danger, fontSize: 13),
              ),
            ),
          GridView.count(
            crossAxisCount: 2,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            mainAxisSpacing: 10,
            crossAxisSpacing: 10,
            childAspectRatio: 1.25,
            children: [
              KpiCard(
                label: 'مبيعات المجموعات',
                value: data.salesDisplay,
                hint: '${data.groupCountDisplay} مجموعة',
                color: AppColors.accent,
                icon: Icons.category_outlined,
              ),
              KpiCard(
                label: 'الفواتير والكمية',
                value: data.invoiceCountDisplay,
                hint: 'كمية ${data.qtyDisplay}',
                icon: Icons.inventory_2_outlined,
              ),
            ],
          ),
          const SizedBox(height: 16),
          const Text(
            'توزيع المجموعات',
            style: TextStyle(
              fontWeight: FontWeight.w800,
              fontSize: 16,
              color: AppColors.brandDark,
            ),
          ),
          const SizedBox(height: 10),
          if (data.rows.isEmpty)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 18),
              child: Text(
                'لا مبيعات مجموعات في الفترة.',
                style: TextStyle(color: AppColors.muted),
              ),
            )
          else
            for (final row in data.rows)
              SalesTile(
                title: row.name,
                subtitle: '${row.invoicesDisplay} فاتورة · كمية ${row.qtyDisplay}',
                amount: row.salesDisplay,
                sharePct: row.sharePct,
                shareDisplay: row.shareDisplay,
              ),
        ],
      ),
    );
  }
}
