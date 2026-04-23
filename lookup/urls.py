from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('paige/', views.paige, name='paige'),
]