from django.urls import path

from . import user_views, views

urlpatterns = [
    path('', views.home, name='home'),
    path('items/', views.item_search, name='item_search'),
    path(
        'items/vendor-item-count/',
        views.item_vendor_item_count,
        name='item_vendor_item_count',
    ),
    path('browse/', views.browse_groups, name='browse_groups'),
    path('inventory/', views.browse_inventory, name='browse_inventory'),
    path('inventory/unsold/', views.browse_unsold, name='browse_unsold'),
    path(
        'inventory/unsold/api/rows/',
        views.browse_unsold_api,
        name='browse_unsold_api',
    ),
    path('inventory/transfers/', views.browse_tr_compare, name='browse_tr_compare'),
    path(
        'inventory/pack-errors/',
        views.browse_inventory_pack_errors,
        name='browse_inventory_pack_errors',
    ),
    path(
        'inventory/transfers/request/',
        views.browse_tr_compare_detail,
        name='browse_tr_compare_detail',
    ),
    path('purchases/', views.browse_purchases, name='browse_purchases'),
    path(
        'purchases/returns/',
        views.browse_purchase_returns,
        name='browse_purchase_returns',
    ),
    path(
        'purchases/returns/api/rows/',
        views.browse_purchase_returns_api,
        name='browse_purchase_returns_api',
    ),
    path(
        'purchases/turnover/',
        views.browse_vendor_turnover,
        name='browse_vendor_turnover',
    ),
    path('purchases/compare/', views.browse_pr_compare, name='browse_pr_compare'),
    path(
        'purchases/compare/request/',
        views.browse_pr_compare_detail,
        name='browse_pr_compare_detail',
    ),
    path('sales/', views.browse_sales, name='browse_sales'),
    path(
        'sales/api/groups/',
        views.browse_sales_groups_api,
        name='browse_sales_groups_api',
    ),
    path(
        'sales/api/groups-month/',
        views.browse_sales_groups_month_api,
        name='browse_sales_groups_month_api',
    ),
    path(
        'sales/api/top-items/',
        views.browse_sales_top_items_api,
        name='browse_sales_top_items_api',
    ),
    path(
        'sales/api/branch-activity/',
        views.browse_sales_branch_activity_api,
        name='browse_sales_branch_activity_api',
    ),
    path(
        'sales/api/top-users/',
        views.browse_sales_top_users_api,
        name='browse_sales_top_users_api',
    ),
    path('suppliers/', views.browse_suppliers, name='browse_suppliers'),
    path('sales/search/', views.sales_search, name='sales_search'),
    path('sales/performance/', views.browse_performance, name='browse_performance'),
    path(
        'sales/no-supply/',
        views.browse_sold_no_supply,
        name='browse_sold_no_supply',
    ),
    path(
        'sales/no-supply/api/rows/',
        views.browse_sold_no_supply_api,
        name='browse_sold_no_supply_api',
    ),
    path('income/', views.browse_income, name='browse_income'),
    path(
        'income/trial-balance/',
        views.browse_trial_balance,
        name='browse_trial_balance',
    ),
    path('income/assets/', views.browse_assets, name='browse_assets'),
    path(
        'income/warehouse-expense/',
        views.browse_warehouse_expense,
        name='browse_warehouse_expense',
    ),
    path('sync-barcodes/', views.sync_barcodes, name='sync_barcodes'),
    path('users/', user_views.user_list, name='user_list'),
    path('users/activity/', user_views.user_activity, name='user_activity'),
    path('users/add/', user_views.user_create, name='user_create'),
    path('users/<int:user_id>/edit/', user_views.user_edit, name='user_edit'),
    path('users/<int:user_id>/delete/', user_views.user_delete, name='user_delete'),
]
