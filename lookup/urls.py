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
    path('warm/', views.warm, name='warm'),
    # RFC 9116 — must live at exactly this path to be discoverable.
    path('.well-known/security.txt', views.security_txt, name='security_txt'),
    path('vehicle-make/', views.vehicle_make, name='vehicle_make'),
    # --- Paid lookup flow (F, paint15). Dormant unless payments_active(). ---
    path('paid/start/', views.start_paid_lookup, name='start_paid_lookup'),
    path('paid/success/', views.paid_success, name='paid_success'),
    path('paid/cancel/', views.paid_cancel, name='paid_cancel'),
    # Register this URL in the Stripe dashboard against the RAILWAY hostname
    # (not the Cloudflare-proxied domain) so bot protection can never challenge
    # Stripe's POSTs. The signature check is what secures it.
    path('stripe/webhook/', views.stripe_webhook, name='stripe_webhook'),
    path('admin-stats/', views.admin_stats, name='admin_stats'),
    path('submit-manual-lookup/', views.submit_manual_lookup, name='submit_manual_lookup'),
    path('dismiss-manual-lookup/', views.dismiss_manual_lookup, name='dismiss_manual_lookup'),
    path('send-compose-email/', views.send_compose_email, name='send_compose_email'),
]