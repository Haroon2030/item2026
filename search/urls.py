from django.urls import path

from . import views

urlpatterns = [
    path('', views.item_search, name='item_search'),
    path('sync-barcodes/', views.sync_barcodes, name='sync_barcodes'),
]
