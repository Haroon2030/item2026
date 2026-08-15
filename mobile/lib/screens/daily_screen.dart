import 'package:flutter/material.dart';

import '../api/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/kpi_card.dart';
import '../widgets/sales_tile.dart';

class DailyScreen extends StatelessWidget {
  const DailyScreen({super.key, required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    final daily = state.daily;
    if (state.loadingDaily && daily == null) {
      return const Center(child: CircularProgressIndicator());
    }
    if (state.dailyError != null && daily == null) {
      return _Message(text: state.dailyError!, onRetry: state.refreshDaily);
    }
    if (daily == null) {
      return const _Message(text: 'لا توجد بيانات لعرضها.');
    }

    final kpis = daily.kpis;
    return RefreshIndicator(
      onRefresh: state.refreshDaily,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 28),
        children: [
          if (daily.fromCache)
            const Padding(
              padding: EdgeInsets.only(bottom: 10),
              child: Text(
                'عرض أرقام محفوظة — تعذّر الاتصال بأوراكل الآن.',
                style: TextStyle(color: AppColors.danger, fontSize: 13),
              ),
            ),
          GridView.count(
            crossAxisCount: 2,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            mainAxisSpacing: 10,
            crossAxisSpacing: 10,
            childAspectRatio: 1.18,
            children: [
              KpiCard(
                label: 'مبيعات نقاط البيع',
                value: kpis.posSalesDisplay,
                hint: '${kpis.posInvoicesDisplay} فاتورة · ${kpis.posBranches} فرع',
                icon: Icons.point_of_sale_outlined,
              ),
              KpiCard(
                label: 'مرتجع نقاط البيع',
                value: kpis.posReturnsDisplay,
                color: AppColors.danger,
                icon: Icons.replay_outlined,
              ),
              KpiCard(
                label: 'نظام المبيعات',
                value: kpis.wholesaleSalesDisplay,
                hint: '${kpis.wholesaleInvoicesDisplay} فاتورة',
                color: AppColors.brandMid,
                icon: Icons.receipt_long_outlined,
              ),
              KpiCard(
                label: 'مبيعات أونكس',
                value: kpis.onixSalesDisplay,
                color: AppColors.accent,
                icon: Icons.analytics_outlined,
              ),
            ],
          ),
          const SizedBox(height: 14),
          if (daily.ranks.isNotEmpty)
            SizedBox(
              height: 112,
              child: ListView(
                scrollDirection: Axis.horizontal,
                children: [
                  for (final key in const [
                    'top_visit_branch',
                    'top_sales_branch',
                    'top_return_branch',
                  ])
                    if (daily.ranks[key] != null)
                      Padding(
                        padding: const EdgeInsets.only(left: 10),
                        child: RankMiniCard(
                          title: daily.ranks[key]!.title,
                          name: daily.ranks[key]!.name,
                          hint:
                              '${daily.ranks[key]!.valueDisplay} · ${daily.ranks[key]!.hint}',
                        ),
                      ),
                ],
              ),
            ),
          const SizedBox(height: 18),
          const _SectionTitle('مبيعات الفروع — نقاط البيع'),
          ..._branchTiles(daily.posBranches),
          const SizedBox(height: 12),
          const _SectionTitle('نظام المبيعات'),
          if (daily.wholesaleBranches.isEmpty)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 12),
              child: Text(
                'لا مبيعات من نظام المبيعات في الفترة.',
                style: TextStyle(color: AppColors.muted),
              ),
            )
          else
            ..._branchTiles(daily.wholesaleBranches),
        ],
      ),
    );
  }

  List<Widget> _branchTiles(List<BranchSales> rows) {
    if (rows.isEmpty) {
      return const [
        Padding(
          padding: EdgeInsets.symmetric(vertical: 12),
          child: Text(
            'لا مبيعات في الفترة.',
            style: TextStyle(color: AppColors.muted),
          ),
        ),
      ];
    }
    return [
      for (final row in rows)
        SalesTile(
          title: row.name,
          subtitle:
              '${row.invoicesDisplay} فاتورة · متوسط سلة ${row.avgBasketDisplay} · مرتجع ${row.returnsDisplay}',
          amount: row.salesDisplay,
          sharePct: row.sharePct,
          shareDisplay: row.shareDisplay,
          dimmed: row.noSales,
        ),
    ];
  }
}

class _SectionTitle extends StatelessWidget {
  const _SectionTitle(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Text(
        text,
        style: const TextStyle(
          fontWeight: FontWeight.w800,
          fontSize: 16,
          color: AppColors.brandDark,
        ),
      ),
    );
  }
}

class _Message extends StatelessWidget {
  const _Message({required this.text, this.onRetry});

  final String text;
  final Future<void> Function()? onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(text, textAlign: TextAlign.center),
            if (onRetry != null) ...[
              const SizedBox(height: 12),
              FilledButton(onPressed: onRetry, child: const Text('إعادة المحاولة')),
            ],
          ],
        ),
      ),
    );
  }
}
