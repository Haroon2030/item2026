from django.urls import path

from . import stock_views, user_views, views

urlpatterns = [
    path('', views.item_search, name='item_search'),
    path('sync-barcodes/', views.sync_barcodes, name='sync_barcodes'),
    path('stock-cost/', stock_views.stock_cost_report, name='stock_cost'),
    path('users/', user_views.user_list, name='user_list'),
    path('users/add/', user_views.user_create, name='user_create'),
    path('users/<int:user_id>/edit/', user_views.user_edit, name='user_edit'),
    path('users/<int:user_id>/delete/', user_views.user_delete, name='user_delete'),
]
