"""DRF serializers for checklist definitions.  Serializers represent and validate; mutations are services."""

from __future__ import annotations

from rest_framework import serializers

from .models import ChecklistCategory, ChecklistItem, ChecklistOption, ChecklistSection, ChecklistTemplate, ChecklistVersion


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
                  "config", "is_required", "is_active", "sort_order", "options")
        read_only_fields = fields


class ChecklistSectionSerializer(serializers.ModelSerializer):
    items = ChecklistItemSerializer(many=True, read_only=True)

    class Meta:
        model = ChecklistSection
        fields = ("id", "name", "description", "instructions", "sort_order", "is_required", "items")
        read_only_fields = fields


class ChecklistVersionSerializer(serializers.ModelSerializer):
    template = serializers.UUIDField(source="template_id", read_only=True)

    class Meta:
        model = ChecklistVersion
        fields = ("id", "template", "version_number", "status", "change_summary", "published_at", "created_at")
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
    change_summary = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")
