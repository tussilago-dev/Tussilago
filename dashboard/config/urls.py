from __future__ import annotations

from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.urls import URLPattern
from django.urls import URLResolver
from django.urls import include
from django.urls import path

from tussilago.views import AcceptOrganizationInvitationView
from tussilago.views import CreateOrganizationView
from tussilago.views import DeclineOrganizationInvitationView
from tussilago.views import IndexView
from tussilago.views import OrganizationDetailView
from tussilago.views import OrganizationInvitationView
from tussilago.views import OrganizationMembersView
from tussilago.views import OrganizationsView
from tussilago.views import ProfileView

admin.autodiscover()

# Require users to login before going to the Django admin site's login page
# This ensures that the admin login page is protected by allauth ratelimiting.
admin.site.login = secure_admin_login(admin.site.login)  # pyright: ignore[reportAttributeAccessIssue]


urlpatterns: list[URLPattern | URLResolver] = [
    # /
    path(
        route="",
        view=IndexView.as_view(),
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
        view=ProfileView.as_view(),
        name="profile",
    ),
    # /organizations/
    path(
        route="organizations/",
        view=OrganizationsView.as_view(),
        name="organizations",
    ),
    # /organizations/create/
    path(
        route="organizations/create/",
        view=CreateOrganizationView.as_view(),
        name="create_organization",
    ),
    # /organizations/<uuid:organization_id>/
    path(
        route="organizations/<uuid:organization_id>/",
        view=OrganizationDetailView.as_view(),
        name="organization_detail",
    ),
    # /organizations/<uuid:organization_id>/members/
    path(
        route="organizations/<uuid:organization_id>/members/",
        view=OrganizationMembersView.as_view(),
        name="organization_members",
    ),
    # /invitations/<str:token>/
    path(
        route="invitations/<str:token>/",
        view=OrganizationInvitationView.as_view(),
        name="handle_org_invitation",
    ),
    # /invitations/<str:token>/accept/
    path(
        route="invitations/<str:token>/accept/",
        view=AcceptOrganizationInvitationView.as_view(),
        name="accept_org_invitation",
    ),
    # /invitations/<str:token>/decline/
    path(
        route="invitations/<str:token>/decline/",
        view=DeclineOrganizationInvitationView.as_view(),
        name="decline_org_invitation",
    ),
    # /i18n/
    path(
        route="i18n/",
        view=include("django.conf.urls.i18n"),
    ),
]
