from __future__ import annotations

from typing import TYPE_CHECKING

from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.urls import URLPattern
from django.urls import URLResolver
from django.urls import include
from django.urls import path

from tussilago.views import index_view
from tussilago.views import profile_view

if TYPE_CHECKING:
    from allauth.urls import URLPattern
    from allauth.urls import URLResolver

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
    path("i18n/", include("django.conf.urls.i18n")),
]
