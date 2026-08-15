class AppUser {
  const AppUser({
    required this.username,
    required this.displayName,
    required this.roleName,
    required this.isStaff,
  });

  final String username;
  final String displayName;
  final String roleName;
  final bool isStaff;

  factory AppUser.fromJson(Map<String, dynamic> json) {
    return AppUser(
      username: '${json['username'] ?? ''}',
      displayName: '${json['display_name'] ?? json['username'] ?? 'مستخدم'}',
      roleName: '${json['role_name'] ?? 'مستخدم'}',
      isStaff: json['is_staff'] == true,
    );
  }
}

class FilterOption {
  const FilterOption({required this.code, required this.name});

  final String code;
  final String name;

  factory FilterOption.fromJson(Map<String, dynamic> json) {
    return FilterOption(
      code: '${json['code'] ?? ''}',
      name: '${json['name'] ?? json['code'] ?? ''}',
    );
  }
}

class RankCard {
  const RankCard({
    required this.title,
    required this.name,
    required this.valueDisplay,
    required this.hint,
  });

  final String title;
  final String name;
  final String valueDisplay;
  final String hint;

  factory RankCard.fromJson(Map<String, dynamic> json) {
    return RankCard(
      title: '${json['title'] ?? ''}',
      name: '${json['name'] ?? '—'}',
      valueDisplay: '${json['value_display'] ?? '—'}',
      hint: '${json['hint'] ?? ''}',
    );
  }
}

class BranchSales {
  const BranchSales({
    required this.code,
    required this.name,
    required this.sales,
    required this.salesDisplay,
    required this.invoices,
    required this.invoicesDisplay,
    required this.returnsDisplay,
    required this.avgBasketDisplay,
    required this.sharePct,
    required this.shareDisplay,
    required this.noSales,
  });

  final String code;
  final String name;
  final double sales;
  final String salesDisplay;
  final int invoices;
  final String invoicesDisplay;
  final String returnsDisplay;
  final String avgBasketDisplay;
  final double sharePct;
  final String shareDisplay;
  final bool noSales;

  factory BranchSales.fromJson(Map<String, dynamic> json) {
    return BranchSales(
      code: '${json['code'] ?? ''}',
      name: '${json['name'] ?? '—'}',
      sales: (json['sales'] as num?)?.toDouble() ?? 0,
      salesDisplay: '${json['sales_display'] ?? '0.00'}',
      invoices: (json['invoices'] as num?)?.toInt() ?? 0,
      invoicesDisplay: '${json['invoices_display'] ?? '0'}',
      returnsDisplay: '${json['returns_display'] ?? '0.00'}',
      avgBasketDisplay: '${json['avg_basket_display'] ?? '0.00'}',
      sharePct: (json['share_pct'] as num?)?.toDouble() ?? 0,
      shareDisplay: '${json['share_display'] ?? '0%'}',
      noSales: json['no_sales'] == true,
    );
  }
}

class DailyKpis {
  const DailyKpis({
    required this.posSalesDisplay,
    required this.posInvoicesDisplay,
    required this.posReturnsDisplay,
    required this.posBranches,
    required this.wholesaleSalesDisplay,
    required this.wholesaleInvoicesDisplay,
    required this.onixSalesDisplay,
    required this.combinedSalesDisplay,
    required this.combinedInvoicesDisplay,
  });

  final String posSalesDisplay;
  final String posInvoicesDisplay;
  final String posReturnsDisplay;
  final int posBranches;
  final String wholesaleSalesDisplay;
  final String wholesaleInvoicesDisplay;
  final String onixSalesDisplay;
  final String combinedSalesDisplay;
  final String combinedInvoicesDisplay;

  factory DailyKpis.fromJson(Map<String, dynamic> json) {
    return DailyKpis(
      posSalesDisplay: '${json['pos_sales_display'] ?? '0.00'}',
      posInvoicesDisplay: '${json['pos_invoices_display'] ?? '0'}',
      posReturnsDisplay: '${json['pos_returns_display'] ?? '0.00'}',
      posBranches: (json['pos_branches'] as num?)?.toInt() ?? 0,
      wholesaleSalesDisplay: '${json['wholesale_sales_display'] ?? '0.00'}',
      wholesaleInvoicesDisplay: '${json['wholesale_invoices_display'] ?? '0'}',
      onixSalesDisplay: '${json['onix_sales_display'] ?? '0.00'}',
      combinedSalesDisplay: '${json['combined_sales_display'] ?? '0.00'}',
      combinedInvoicesDisplay: '${json['combined_invoices_display'] ?? '0'}',
    );
  }
}

class DailySales {
  const DailySales({
    required this.dateFrom,
    required this.dateTo,
    required this.periodLabel,
    required this.scopeLabel,
    required this.fromCache,
    required this.kpis,
    required this.posBranches,
    required this.wholesaleBranches,
    required this.ranks,
  });

  final String dateFrom;
  final String dateTo;
  final String periodLabel;
  final String scopeLabel;
  final bool fromCache;
  final DailyKpis kpis;
  final List<BranchSales> posBranches;
  final List<BranchSales> wholesaleBranches;
  final Map<String, RankCard> ranks;

  factory DailySales.fromJson(Map<String, dynamic> json) {
    final ranksJson = (json['ranks'] as Map?)?.cast<String, dynamic>() ?? {};
    return DailySales(
      dateFrom: '${json['date_from'] ?? ''}',
      dateTo: '${json['date_to'] ?? ''}',
      periodLabel: '${json['period_label'] ?? ''}',
      scopeLabel: '${json['scope_label'] ?? ''}',
      fromCache: json['from_cache'] == true,
      kpis: DailyKpis.fromJson(
        (json['kpis'] as Map?)?.cast<String, dynamic>() ?? {},
      ),
      posBranches: [
        for (final row in (json['pos_branches'] as List? ?? []))
          if (row is Map)
            BranchSales.fromJson(row.cast<String, dynamic>()),
      ],
      wholesaleBranches: [
        for (final row in (json['wholesale_branches'] as List? ?? []))
          if (row is Map)
            BranchSales.fromJson(row.cast<String, dynamic>()),
      ],
      ranks: {
        for (final entry in ranksJson.entries)
          if (entry.value is Map)
            entry.key: RankCard.fromJson(
              (entry.value as Map).cast<String, dynamic>(),
            ),
      },
    );
  }
}

class GroupSalesRow {
  const GroupSalesRow({
    required this.code,
    required this.name,
    required this.sales,
    required this.salesDisplay,
    required this.invoicesDisplay,
    required this.qtyDisplay,
    required this.sharePct,
    required this.shareDisplay,
  });

  final String code;
  final String name;
  final double sales;
  final String salesDisplay;
  final String invoicesDisplay;
  final String qtyDisplay;
  final double sharePct;
  final String shareDisplay;

  factory GroupSalesRow.fromJson(Map<String, dynamic> json) {
    return GroupSalesRow(
      code: '${json['code'] ?? json['group_code'] ?? ''}',
      name: '${json['name'] ?? json['group_name'] ?? '—'}',
      sales: (json['sales_total'] as num?)?.toDouble() ?? 0,
      salesDisplay: '${json['sales_total_display'] ?? '0.00'}',
      invoicesDisplay: '${json['invoice_count_display'] ?? '0'}',
      qtyDisplay: '${json['qty_display'] ?? '0'}',
      sharePct: (json['share_pct'] as num?)?.toDouble() ?? 0,
      shareDisplay: '${json['share_display'] ?? '0%'}',
    );
  }
}

class GroupSales {
  const GroupSales({
    required this.rows,
    required this.salesDisplay,
    required this.groupCountDisplay,
    required this.invoiceCountDisplay,
    required this.qtyDisplay,
    this.warning = '',
  });

  final List<GroupSalesRow> rows;
  final String salesDisplay;
  final String groupCountDisplay;
  final String invoiceCountDisplay;
  final String qtyDisplay;
  final String warning;

  factory GroupSales.fromJson(Map<String, dynamic> json) {
    final totals = (json['totals'] as Map?)?.cast<String, dynamic>() ?? {};
    return GroupSales(
      rows: [
        for (final row in (json['rows'] as List? ?? []))
          if (row is Map)
            GroupSalesRow.fromJson(row.cast<String, dynamic>()),
      ],
      salesDisplay: '${totals['sales_total_display'] ?? '0.00'}',
      groupCountDisplay: '${totals['group_count_display'] ?? '0'}',
      invoiceCountDisplay: '${totals['invoice_count_display'] ?? '0'}',
      qtyDisplay: '${totals['qty_display'] ?? '0'}',
      warning: '${json['warning'] ?? ''}',
    );
  }
}

class ApiException implements Exception {
  ApiException(this.message, {this.statusCode});

  final String message;
  final int? statusCode;

  bool get needsLogin => statusCode == 401;

  @override
  String toString() => message;
}
