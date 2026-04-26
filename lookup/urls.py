from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('results/', views.results, name='results'),
    path('submit-email/', views.submit_email, name='submit_email'),
    path('paige/', views.paige, name='paige'),
    path('info/', views.info, name='info'),
    path('submit-contact/', views.submit_contact, name='submit_contact'),
]