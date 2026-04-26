from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('results/', views.results, name='results'),
    path('submit-email/', views.submit_email, name='submit_email'),
    path('paige/', views.paige, name='paige'),
]