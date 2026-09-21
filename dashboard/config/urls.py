from __future__ import annotations

from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.urls import URLPattern
from django.urls import URLResolver
from django.urls import include
from django.urls import path

from tussilago.views import create_organization
from tussilago.views import index_view
from tussilago.views import organization_detail
from tussilago.views import organizations
from tussilago.views import profile_view

admin.autodiscover()

# Require users to login before going to the Django admin site's login page
# This ensures that the admin login page is protected by allauth ratelimiting.
admin.site.login = secure_admin_login(admin.site.login)  # pyright: ignore[reportAttributeAccessIssue]


urlpatterns: list[URLPattern | URLResolver] = [
    path(route="", view=index_view, name="index"),
    path("", include("allauth.idp.urls")),
    path(route="admin/", view=admin.site.urls),
    path(route="accounts/", view=include("allauth.urls")),
    path(route="mfa/", view=include("allauth.mfa.urls")),
    path(route="profile/", view=profile_view, name="profile"),
    path(route="organizations/", view=organizations, name="organizations"),
    path(route="organizations/create/", view=create_organization, name="create_organization"),
    path(route="organizations/<uuid:organization_id>/", view=organization_detail, name="organization_detail"),
    path("i18n/", include("django.conf.urls.i18n")),
]
