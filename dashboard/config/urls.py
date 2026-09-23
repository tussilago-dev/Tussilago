from __future__ import annotations

from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.urls import URLPattern
from django.urls import URLResolver
from django.urls import include
from django.urls import path

from tussilago.views import accept_organization_invitation
from tussilago.views import create_organization
from tussilago.views import decline_organization_invitation
from tussilago.views import index
from tussilago.views import organization_detail
from tussilago.views import organization_invitation
from tussilago.views import organization_members
from tussilago.views import organizations
from tussilago.views import profile

admin.autodiscover()

# Require users to login before going to the Django admin site's login page
# This ensures that the admin login page is protected by allauth ratelimiting.
admin.site.login = secure_admin_login(admin.site.login)  # pyright: ignore[reportAttributeAccessIssue]


urlpatterns: list[URLPattern | URLResolver] = [
    # /
    path(
        route="",
        view=index,
        name="index",
    ),
    # /admin/
    path(
        route="admin/",
        view=admin.site.urls,
    ),
    # /accounts/
    path(
        route="accounts/",
        view=include("allauth.urls"),
    ),
    # /mfa/
    path(
        route="mfa/",
        view=include("allauth.mfa.urls"),
    ),
    # /profile/
    path(
        route="profile/",
        view=profile,
        name="profile",
    ),
    # /organizations/
    path(
        route="organizations/",
        view=organizations,
        name="organizations",
    ),
    # /organizations/create/
    path(
        route="organizations/create/",
        view=create_organization,
        name="create_organization",
    ),
    # /organizations/<uuid:organization_id>/
    path(
        route="organizations/<uuid:organization_id>/",
        view=organization_detail,
        name="organization_detail",
    ),
    # /organizations/<uuid:organization_id>/members/
    path(
        route="organizations/<uuid:organization_id>/members/",
        view=organization_members,
        name="organization_members",
    ),
    # /invitations/<str:token>/
    path(
        route="invitations/<str:token>/",
        view=organization_invitation,
        name="handle_org_invitation",
    ),
    # /invitations/<str:token>/accept/
    path(
        route="invitations/<str:token>/accept/",
        view=accept_organization_invitation,
        name="accept_org_invitation",
    ),
    # /invitations/<str:token>/decline/
    path(
        route="invitations/<str:token>/decline/",
        view=decline_organization_invitation,
        name="decline_org_invitation",
    ),
    # /i18n/
    path(
        route="i18n/",
        view=include("django.conf.urls.i18n"),
    ),
]
