from django.urls import path

from . import user_views, views

urlpatterns = [
    path('', views.item_search, name='item_search'),
    path('browse/', views.browse_groups, name='browse_groups'),
    path('inventory/', views.browse_inventory, name='browse_inventory'),
    path('purchases/', views.browse_purchases, name='browse_purchases'),
    path('sales/', views.browse_sales, name='browse_sales'),
    path('sales/performance/', views.browse_performance, name='browse_performance'),
    path('income/', views.browse_income, name='browse_income'),
    path('sales/api/groups/', views.browse_sales_groups_api, name='browse_sales_groups_api'),
    path('sales/api/items/', views.browse_sales_items_api, name='browse_sales_items_api'),
    path('sales/api/users/', views.browse_sales_users_api, name='browse_sales_users_api'),
    path('sales/api/panels/', views.browse_sales_panels_api, name='browse_sales_panels_api'),
    path('sales/api/charts/', views.browse_sales_charts_api, name='browse_sales_charts_api'),
    path('sales/api/margins/', views.browse_sales_margins_api, name='browse_sales_margins_api'),
    path('sync-barcodes/', views.sync_barcodes, name='sync_barcodes'),
    path('users/', user_views.user_list, name='user_list'),
    path('users/add/', user_views.user_create, name='user_create'),
    path('users/<int:user_id>/edit/', user_views.user_edit, name='user_edit'),
    path('users/<int:user_id>/delete/', user_views.user_delete, name='user_delete'),
]
