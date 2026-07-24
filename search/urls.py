from django.urls import path

from . import views

urlpatterns = [
    path('', views.item_search, name='item_search'),
    path('sync-barcodes/', views.sync_barcodes, name='sync_barcodes'),
    path('__agent_dbg/', views.agent_debug_ingest, name='agent_debug_ingest'),
    path('__agent_dbg/dump/', views.agent_debug_dump, name='agent_debug_dump'),
]
