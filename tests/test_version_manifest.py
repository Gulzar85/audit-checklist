"""The version manifest and integrity hash: deterministic, meaningful, never trusted without re-derivation."""

from __future__ import annotations

import json

import pytest

from apps.checklists import guards, lifecycle, manifest, services
from apps.checklists.models import ChecklistItem, ChecklistVersion

from versioning_support import build_world, item_of


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign, publish=True)


@pytest.mark.django_db
class TestDeterminism:
    def test_the_same_rows_give_the_same_hash(self, world):
        assert manifest.manifest_hash(world["v1"]) == manifest.manifest_hash(world["v1"]) == world["v1"].integrity_hash

    def test_a_clone_has_the_same_hash_although_every_id_differs(self, world):
        v2 = lifecycle.create_draft(world["template"], actor=world["author"])
        assert manifest.manifest_hash(v2) == manifest.manifest_hash(world["v1"])

    def test_volatile_fields_are_excluded(self, world):
        v2 = lifecycle.create_draft(world["template"], actor=world["author"])
        before = manifest.manifest_hash(v2)
        item = item_of(v2, "FIRE-EXIT")
        ChecklistItem.objects.filter(pk=item.pk).update(updated_at=item.updated_at.replace(year=2001))
        services.update_checklist_template(world["template"], actor=world["author"], name="Another name", description="x")
        assert manifest.manifest_hash(v2) == before

    def test_key_order_inside_json_does_not_matter(self, world):
        v2 = lifecycle.create_draft(world["template"], actor=world["author"])
        before = manifest.manifest_hash(v2)
        item = item_of(v2, "FRIDGE-TEMP")
        ChecklistItem.objects.filter(pk=item.pk).update(config=dict(reversed(list(item.config.items()))))
        assert manifest.manifest_hash(v2) == before

    @pytest.mark.parametrize("change", ["question", "answer_type_config", "option", "rule", "section_order", "item_order", "required", "new_section"])
    def test_every_behavioural_change_changes_the_hash(self, world, change):
        author = world["author"]
        v2 = lifecycle.create_draft(world["template"], actor=author)
        before = manifest.manifest_hash(v2)
        if change == "question":
            services.update_item(item_of(v2, "FIRE-EXIT"), actor=author, question="Different wording")
        elif change == "answer_type_config":
            services.update_item(item_of(v2, "FRIDGE-TEMP"), actor=author, config={"min": 0, "max": 8, "decimals": 1, "unit": "°C"})
        elif change == "option":
            services.create_option(item_of(v2, "CONDITION"), label="Excellent", actor=author)
        elif change == "rule":
            services.update_rule(v2.rules.get(), actor=author, actions=[{"type": "SHOW_ITEM", "target": {"type": "item", "code": "ISSUE"}}])
        elif change == "section_order":
            ids = list(v2.sections.order_by("sort_order").values_list("pk", flat=True))
            services.reorder_sections(v2, [ids[1], ids[0], ids[2]], actor=author)
        elif change == "item_order":
            services.move_item(item_of(v2, "FRIDGE-TEMP"), "up", actor=author)
        elif change == "required":
            services.update_item(item_of(v2, "FIRE-EXIT"), actor=author, is_required=False)
        else:
            services.create_section(v2, name="Another", actor=author)
        assert manifest.manifest_hash(v2) != before


@pytest.mark.django_db
class TestCanonicalForm:
    def test_the_manifest_shape(self, world):
        data = manifest.build_manifest(world["v1"])
        assert data["schema"] == 1 and [s["code"] for s in data["sections"]] == ["S-001", "S-002", "S-003"]
        food = data["sections"][1]
        assert [i["code"] for i in food["items"]] == ["HANDWASH", "FRIDGE-TEMP", "CONDITION"]
        assert [o["value"] for o in food["items"][2]["options"]] == ["good", "fair", "poor"]
        assert data["rules"][0]["code"] == "R-001" and set(data["rules"][0]) == {"code", "name", "description", "condition", "actions", "is_active"}
        text = json.dumps(data)
        assert '"id"' not in text and "created" not in text and "sort_order" not in text and "updated" not in text

    def test_canonical_json_is_compact_sorted_utf8(self, world):
        text = manifest.canonical_json(manifest.build_manifest(world["v1"]))
        assert ": " not in text and ", " not in text and "°C" in text and "\\u00b0" not in text
        assert json.loads(text)["schema"] == 1
        assert text == manifest.canonical_json(json.loads(text))  # stable under a round trip

    def test_the_hash_format(self, world):
        value = manifest.manifest_hash(world["v1"])
        algorithm, schema, digest = value.split(":")
        assert (algorithm, schema) == ("sha256", "v1") and len(digest) == 64 and int(digest, 16) >= 0

    def test_four_queries_whatever_the_size(self, world, django_assert_max_num_queries):
        with django_assert_max_num_queries(5):
            manifest.build_manifest(world["v1"])


@pytest.mark.django_db
class TestVerification:
    def test_verified_draft_unsealed_unverifiable_and_mismatch(self, world):
        v1 = ChecklistVersion.objects.get(pk=world["v1"].pk)
        assert manifest.verify_version_integrity(v1).status == "verified" and manifest.verify_version_integrity(v1).ok
        draft = lifecycle.create_draft(world["template"], actor=world["author"])
        assert manifest.verify_version_integrity(draft).status == "draft"
        for stored, expected in (("", "unsealed"), ("sha256:v9:" + "0" * 64, "unverifiable"), ("md5:v1:abc", "unverifiable"), ("garbage", "unverifiable"),
                                 ("sha256:v1:" + "0" * 64, "mismatch")):
            v1.integrity_hash = stored
            assert manifest.verify_version_integrity(v1).status == expected, stored

    def test_a_stored_hash_is_never_trusted(self, world):
        """Tampering with the rows *and* recomputing nothing: the stored hash no longer matches what is derived."""
        v1 = ChecklistVersion.objects.get(pk=world["v1"].pk)
        with guards.allow_definition_changes():
            ChecklistItem.objects.filter(section__version=v1, code="ISSUE").update(question="Quietly changed")
        result = manifest.verify_version_integrity(v1)
        assert result.status == "mismatch" and result.actual != result.expected and not result.ok

    def test_labels_are_human_readable(self):
        assert manifest.IntegrityResult("mismatch").label.startswith("Definition differs")
        assert "unknown manifest schema" in manifest.IntegrityResult("unverifiable").label
