"""Branding uploads: type, size, content, dimensions and filename handling."""

from __future__ import annotations

import io
import re

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from apps.organizations import services
from apps.organizations.validators import MAX_BRANDING_IMAGE_BYTES

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    """Write uploads to a throw-away directory."""
    settings.MEDIA_ROOT = tmp_path / "media"
    return tmp_path / "media"


def image_bytes(fmt="PNG", size=(16, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def upload(name="logo.png", content=None, content_type="image/png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content if content is not None else image_bytes(), content_type=content_type)


class TestValidation:
    @pytest.mark.parametrize(
        ("name", "fmt"),
        [("a.png", "PNG"), ("a.jpg", "JPEG"), ("a.jpeg", "JPEG"), ("a.webp", "WEBP"), ("a.gif", "GIF"), ("A.PNG", "PNG")],
    )
    def test_real_images_are_accepted(self, org_a, name, fmt):
        org = services.update_organization(org_a, logo=upload(name, image_bytes(fmt)))
        assert org.logo and org.logo.size > 0

    @pytest.mark.parametrize("name", ["evil.exe", "evil.php", "evil.js", "evil.html", "evil.svg", "evil.bat", "evil", "evil.png.exe"])
    def test_disallowed_extensions_are_rejected(self, org_a, name):
        with pytest.raises(ValidationError) as exc:
            services.update_organization(org_a, logo=upload(name, image_bytes()))
        assert "logo" in exc.value.message_dict

    def test_svg_is_rejected_even_with_an_image_mime_type(self, org_a):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        with pytest.raises(ValidationError):
            services.update_organization(org_a, logo=upload("logo.svg", svg, "image/svg+xml"))

    @pytest.mark.parametrize(
        "payload",
        [b"MZ\x90\x00 this is an executable pretending to be an image", b"<?php system($_GET['c']); ?>", b"just text", b""],
    )
    def test_content_is_verified_not_the_extension_or_mime_type(self, org_a, payload):
        with pytest.raises(ValidationError) as exc:
            services.update_organization(org_a, logo=upload("logo.png", payload, "image/png"))
        assert "logo" in exc.value.message_dict

    def test_a_script_polyglot_is_not_an_image(self, org_a):
        polyglot = b"GIF89a/*" + b"\x00" * 10 + b"*/=1;alert(1);"
        with pytest.raises(ValidationError):
            services.update_organization(org_a, logo=upload("logo.gif", polyglot, "image/gif"))

    def test_oversized_files_are_rejected(self, org_a):
        big = image_bytes() + b"\x00" * (MAX_BRANDING_IMAGE_BYTES + 1)
        with pytest.raises(ValidationError) as exc:
            services.update_organization(org_a, logo=upload("big.png", big))
        assert "too large" in str(exc.value.message_dict["logo"])

    def test_extreme_dimensions_are_rejected(self, org_a):
        with pytest.raises(ValidationError) as exc:
            services.update_organization(org_a, logo=upload("wide.png", image_bytes(size=(5000, 4))))
        assert "pixels" in str(exc.value.message_dict["logo"])

    def test_a_truncated_image_is_rejected(self, org_a):
        with pytest.raises(ValidationError):
            services.update_organization(org_a, logo=upload("cut.png", image_bytes()[:30]))

    def test_the_favicon_gets_the_same_checks(self, org_a):
        with pytest.raises(ValidationError) as exc:
            services.update_organization(org_a, favicon=upload("f.exe", image_bytes()))
        assert "favicon" in exc.value.message_dict


class TestStorage:
    def test_stored_name_is_generated_not_client_supplied(self, org_a, media):
        org = services.update_organization(org_a, logo=upload("My Company Logo (final).PNG"))
        assert re.fullmatch(rf"organizations/{org.pk}/logo/[0-9a-f]{{32}}\.png", org.logo.name)
        assert (media / org.logo.name).is_file()

    def test_path_traversal_in_the_filename_is_neutralised(self, org_a, media):
        org = services.update_organization(org_a, logo=upload("../../../etc/evil.png"))
        assert ".." not in org.logo.name and org.logo.name.startswith(f"organizations/{org.pk}/logo/")
        assert not any(p.name == "evil.png" for p in media.rglob("*"))

    def test_double_extensions_do_not_survive(self, org_a):
        org = services.update_organization(org_a, logo=upload("shell.php.png"))
        assert "php" not in org.logo.name

    def test_logo_and_favicon_use_separate_folders(self, org_a):
        org = services.update_organization(org_a, logo=upload("a.png"), favicon=upload("b.png"))
        assert "/logo/" in org.logo.name and "/favicon/" in org.favicon.name

    def test_a_replacement_gets_a_new_name(self, org_a):
        first = services.update_organization(org_a, logo=upload("a.png")).logo.name
        second = services.update_organization(org_a, logo=upload("a.png")).logo.name
        assert first != second

    def test_the_change_is_audited_without_exposing_paths(self, org_a, staff_user):
        from apps.core.models import ActivityLog

        services.update_organization(org_a, actor=staff_user, logo=upload("a.png"))
        change = ActivityLog.objects.get(verb="organization.updated").metadata["changes"]["logo"]
        assert change == {"from": "", "to": "(new file)"}

    def test_clearing_the_logo(self, org_a):
        services.update_organization(org_a, logo=upload("a.png"))
        org = services.update_organization(org_a, logo=False)
        assert not org.logo

    def test_an_unchanged_stored_file_is_left_alone(self, org_a):
        org = services.update_organization(org_a, logo=upload("a.png"))
        name = org.logo.name
        org = services.update_organization(org, logo=org.logo, name="Renamed")  # a form re-submits the stored file
        assert org.logo.name == name


class TestEntryPoints:
    def test_branding_form_accepts_an_image(self, client, admin_a, org_a):
        client.force_login(admin_a)
        response = client.post(
            reverse("organizations:branding", args=[org_a.pk]),
            {"logo": upload("logo.png"), "primary_color": "#112233"},
        )
        assert response.status_code == 302
        org_a.refresh_from_db()
        assert org_a.logo and org_a.primary_color == "#112233"

    def test_branding_form_rejects_a_disguised_file_with_a_clear_message(self, client, admin_a, org_a):
        client.force_login(admin_a)
        response = client.post(
            reverse("organizations:branding", args=[org_a.pk]), {"logo": upload("logo.png", b"MZ not an image")}
        )
        assert response.status_code == 200
        assert "valid image" in response.content.decode()
        org_a.refresh_from_db()
        assert not org_a.logo

    def test_branding_form_rejects_svg(self, client, admin_a, org_a):
        client.force_login(admin_a)
        response = client.post(reverse("organizations:branding", args=[org_a.pk]), {"logo": upload("logo.svg", b"<svg/>")})
        assert response.status_code == 200 and "valid image" in response.content.decode()
        org_a.refresh_from_db()
        assert not org_a.logo

    def test_branding_form_can_clear_the_logo(self, client, admin_a, org_a):
        services.update_organization(org_a, logo=upload("a.png"))
        client.force_login(admin_a)
        client.post(reverse("organizations:branding", args=[org_a.pk]), {"logo-clear": "on"})
        org_a.refresh_from_db()
        assert not org_a.logo

    def test_the_logo_is_displayed_on_the_detail_page(self, client, admin_a, org_a):
        org = services.update_organization(org_a, logo=upload("a.png"))
        client.force_login(admin_a)
        body = client.get(reverse("organizations:detail", args=[org.pk])).content.decode()
        assert org.logo.url in body and f'alt="Logo of {org.name}"' in body

    def test_api_multipart_upload_and_validation(self, admin_a, org_a):
        api = APIClient()
        api.force_authenticate(admin_a)
        url = f"/api/v1/organizations/{org_a.pk}/"
        ok = api.patch(url, {"logo": upload("a.png")}, format="multipart")
        assert ok.status_code == 200 and "/logo/" in ok.json()["data"]["logo"]
        bad = api.patch(url, {"logo": upload("a.png", b"not an image")}, format="multipart")
        assert bad.status_code == 400 and "logo" in bad.json()["error"]["details"]
        exe = api.patch(url, {"logo": upload("x.exe", image_bytes())}, format="multipart")
        assert exe.status_code == 400

    def test_api_can_clear_the_logo_with_null(self, admin_a, org_a):
        services.update_organization(org_a, logo=upload("a.png"))
        api = APIClient()
        api.force_authenticate(admin_a)
        response = api.patch(f"/api/v1/organizations/{org_a.pk}/", {"logo": None}, format="json")
        assert response.status_code == 200 and response.json()["data"]["logo"] is None

    def test_read_only_members_cannot_upload(self, viewer_a, org_a):
        api = APIClient()
        api.force_authenticate(viewer_a)
        response = api.patch(f"/api/v1/organizations/{org_a.pk}/", {"logo": upload("a.png")}, format="multipart")
        assert response.status_code == 403
        org_a.refresh_from_db()
        assert not org_a.logo
