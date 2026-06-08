from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('results/', views.results, name='results'),
    path('lookup-status/<int:search_id>/', views.lookup_status, name='lookup_status'),
    path('submit-email/', views.submit_email, name='submit_email'),
    path('paige/', views.paige, name='paige'),
    path('about/', views.about, name='about'),
    path('privacy/', views.privacy, name='privacy'),
    path('disclaimer/', views.disclaimer, name='disclaimer'),
    path('help/', views.help_page, name='help'),
    path('submit-contact/', views.submit_contact, name='submit_contact'),
    path('admin-stats/', views.admin_stats, name='admin_stats'),
    path('submit-manual-lookup/', views.submit_manual_lookup, name='submit_manual_lookup'),
    path('dismiss-manual-lookup/', views.dismiss_manual_lookup, name='dismiss_manual_lookup'),
    path('send-compose-email/', views.send_compose_email, name='send_compose_email'),
]