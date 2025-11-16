from django.urls import path
from . import views

app_name = "campaigns"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),

    # Campaigns
    path("campaigns/", views.campaign_list, name="campaign_list"),
    path("campaigns/new/", views.campaign_create, name="campaign_create"),
    path("campaigns/<int:pk>/", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<int:pk>/edit/", views.campaign_update, name="campaign_update"),
    path("campaigns/<int:pk>/delete/", views.campaign_delete, name="campaign_delete"),
    path("campaigns/<int:pk>/trigger/", views.campaign_trigger_now, name="campaign_trigger_now"),

    # Recipients
    path("recipients/upload/", views.recipient_upload, name="recipient_upload"),
    path("recipients/<int:pk>/edit/", views.recipient_edit, name="recipient_edit"),
    path("recipients/<int:pk>/delete/", views.recipient_delete, name="recipient_delete"),
    path("bounces/", views.bounce_list, name="bounce_list"),
    path("bounces/report.csv", views.bounce_report_csv, name="bounce_report_csv"),
    path("admin/email-settings/", views.email_settings_view, name="email_settings"),

]
