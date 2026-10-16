"""DRF serializers for checklist definitions.  Serializers represent and validate; mutations are services."""

from __future__ import annotations

import json

from rest_framework import serializers

from .rules import limit

from .models import (
    ChecklistCategory,
    ChecklistItem,
    ChecklistOption,
    ChecklistRule,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistVersion,
)


class ChecklistOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChecklistOption
        fields = ("id", "value", "label", "sort_order", "is_active")
        read_only_fields = fields


class ChecklistItemSerializer(serializers.ModelSerializer):
    options = ChecklistOptionSerializer(many=True, read_only=True)
    section = serializers.UUIDField(source="section_id", read_only=True)

    class Meta:
        model = ChecklistItem
        fields = ("id", "section", "code", "question", "description", "help_text", "instructions", "answer_type",
                  "config", "metadata", "is_required", "is_active", "sort_order", "options")
        read_only_fields = fields


class ChecklistSectionSerializer(serializers.ModelSerializer):
    items = ChecklistItemSerializer(many=True, read_only=True)

    class Meta:
        model = ChecklistSection
        fields = ("id", "name", "description", "instructions", "sort_order", "is_required", "items")
        read_only_fields = fields


class ChecklistVersionSerializer(serializers.ModelSerializer):
    """A version and its lifecycle record.  ``section_count`` / ``item_count`` appear when the queryset is annotated."""

    template = serializers.UUIDField(source="template_id", read_only=True)
    based_on = serializers.IntegerField(source="based_on.version_number", read_only=True, default=None)
    created_by = serializers.UUIDField(source="created_by_id", read_only=True, default=None)
    published_by = serializers.UUIDField(source="published_by_id", read_only=True, default=None)
    section_count = serializers.IntegerField(read_only=True, default=None)
    item_count = serializers.IntegerField(read_only=True, default=None)

    class Meta:
        model = ChecklistVersion
        fields = (
            "id", "template", "version_number", "status", "change_summary", "based_on", "created_by", "created_at",
            "published_at", "published_by", "superseded_at", "integrity_hash", "section_count", "item_count",
        )
        read_only_fields = fields


class ChecklistCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ChecklistCategory
        fields = ("id", "name", "sort_order")
        read_only_fields = fields


class ChecklistTemplateSerializer(serializers.ModelSerializer):
    organization = serializers.UUIDField(source="organization_id", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    latest_version = serializers.IntegerField(source="latest_version_number", read_only=True, default=None)
    published_version = serializers.IntegerField(source="published_version_number", read_only=True, default=None)

    class Meta:
        model = ChecklistTemplate
        fields = ("id", "organization", "code", "slug", "name", "description", "instructions",
                  "estimated_duration_minutes", "status", "category", "category_name", "latest_version",
                  "published_version", "created_at", "updated_at")
        read_only_fields = ("id", "organization", "slug", "status", "category_name", "latest_version",
                            "published_version", "created_at", "updated_at")


class ChecklistTemplateWriteSerializer(serializers.Serializer):
    """Input for create / update.  ``organization`` and ``code`` are accepted on create only."""

    organization = serializers.UUIDField(required=False)
    code = serializers.CharField(max_length=40, required=False)
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    instructions = serializers.CharField(required=False, allow_blank=True)
    estimated_duration_minutes = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=1440)
    category = serializers.UUIDField(required=False, allow_null=True)


class DuplicateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    code = serializers.CharField(max_length=40, required=False)


class NewVersionSerializer(serializers.Serializer):
    """Create a draft: ``source`` (a version id of this checklist; default: the current published version) is optional."""

    change_summary = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")
    source = serializers.UUIDField(required=False)


class PublishSerializer(serializers.Serializer):
    """Publish a draft.  ``expected_hash`` is the ``draft_hash`` the client last validated (optimistic concurrency)."""

    change_summary = serializers.CharField(max_length=300, required=False, allow_blank=True)
    expected_hash = serializers.CharField(max_length=80, required=False, allow_blank=True)


class ChecklistItemWriteSerializer(serializers.Serializer):
    """Input for creating / updating an item.  It only checks *shape*; the answer-type engine (through the
    service) validates ``answer_type``, ``config`` and ``options`` exactly as the builder does."""

    section = serializers.UUIDField(required=False)
    question = serializers.CharField(max_length=500, required=False)
    code = serializers.CharField(max_length=40, required=False, allow_blank=True)
    answer_type = serializers.CharField(max_length=20, required=False)
    config = serializers.JSONField(required=False, allow_null=True)
    metadata = serializers.JSONField(required=False, allow_null=True)
    is_required = serializers.BooleanField(required=False)
    is_active = serializers.BooleanField(required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    help_text = serializers.CharField(max_length=500, required=False, allow_blank=True)
    instructions = serializers.CharField(required=False, allow_blank=True)
    options = serializers.ListField(child=serializers.JSONField(), required=False, max_length=100)


class ChecklistRuleSerializer(serializers.ModelSerializer):
    """A stored rule plus a readable summary (``context["snapshot"]`` supplies the question texts)."""

    summary = serializers.SerializerMethodField()
    version = serializers.IntegerField(source="version.version_number", read_only=True)

    class Meta:
        model = ChecklistRule
        fields = ("id", "code", "name", "description", "condition", "actions", "sort_order", "is_active", "version", "summary")
        read_only_fields = fields

    def get_summary(self, rule: ChecklistRule) -> str:
        from .rules import describe_rule

        snapshot = self.context.get("snapshot")
        return describe_rule(snapshot, rule.condition, rule.actions) if snapshot is not None else ""


def _bounded_json(value, what: str, maximum: int = 60_000):  # noqa: ANN001, ANN202
    if len(json.dumps(value, default=str)) > maximum:
        raise serializers.ValidationError(f"{what} is too large.")
    return value


class RuleWriteSerializer(serializers.Serializer):
    """Shape only: the rule validator (through the service) judges the content, exactly as for the builder."""

    code = serializers.CharField(max_length=40, required=False, allow_blank=True)
    name = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(max_length=300, required=False, allow_blank=True)
    condition = serializers.JSONField(required=False, allow_null=True)
    actions = serializers.JSONField(required=False)
    is_active = serializers.BooleanField(required=False)

    def validate_condition(self, value):  # noqa: ANN001, ANN201
        return _bounded_json(value, "The condition")

    def validate_actions(self, value):  # noqa: ANN001, ANN201
        if not isinstance(value, list):
            raise serializers.ValidationError("Actions must be a list.")
        return _bounded_json(value, "The actions")


class RuleValidateSerializer(serializers.Serializer):
    version = serializers.IntegerField(required=False, min_value=1)
    rules = serializers.ListField(child=serializers.JSONField(), required=False, max_length=500)


class SimulationSerializer(serializers.Serializer):
    """A bounded simulation request: answers by question code (raw values) and a list of N/A question codes."""

    version = serializers.IntegerField(required=False, min_value=1)
    answers = serializers.DictField(child=serializers.JSONField(allow_null=True), required=False)
    not_applicable = serializers.ListField(child=serializers.CharField(max_length=40), required=False)
    rules = serializers.ListField(child=serializers.JSONField(), required=False, max_length=500)

    def validate_answers(self, value):  # noqa: ANN001, ANN201
        if len(value) > limit("max_simulation_answers"):
            raise serializers.ValidationError(f"At most {limit('max_simulation_answers')} answers may be simulated.")
        for code, raw in value.items():
            if len(code) > 40 or len(json.dumps(raw, default=str)) > limit("max_simulation_value_chars"):
                raise serializers.ValidationError(f"The value for “{code[:40]}” is too large.")
        return value

    def validate_not_applicable(self, value):  # noqa: ANN001, ANN201
        if len(value) > limit("max_simulation_answers"):
            raise serializers.ValidationError("Too many entries.")
        return value
