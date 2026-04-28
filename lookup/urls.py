from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('results/', views.results, name='results'),
    path('submit-email/', views.submit_email, name='submit_email'),
    path('paige/', views.paige, name='paige'),
    path('about/', views.about, name='about'),
    path('help/', views.help_page, name='help'),
    path('submit-contact/', views.submit_contact, name='submit_contact'),
    path('admin-stats/', views.admin_stats, name='admin_stats'),
]