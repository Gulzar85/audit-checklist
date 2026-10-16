"""DRF representation of the registry's metadata (read-only; the registry is the source of truth)."""

from __future__ import annotations

from rest_framework import serializers


class CapabilitiesSerializer(serializers.Serializer):
    requires_options = serializers.BooleanField()
    supports_options = serializers.BooleanField()
    has_fixed_options = serializers.BooleanField()
    supports_comment = serializers.BooleanField()
    supports_evidence = serializers.BooleanField()
    supports_score = serializers.BooleanField()
    supports_rules = serializers.BooleanField()
    produces_scalar = serializers.BooleanField()
    supports_numeric_validation = serializers.BooleanField()
    is_media = serializers.BooleanField()


class ConfigFieldSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    kind = serializers.CharField()
    default = serializers.JSONField(allow_null=True)
    help = serializers.CharField(allow_blank=True)
    min = serializers.FloatField(allow_null=True)
    max = serializers.FloatField(allow_null=True)
    max_length = serializers.IntegerField(allow_null=True)
    max_items = serializers.IntegerField(allow_null=True)
    choices = serializers.ListField(child=serializers.DictField())
    allowed_values = serializers.ListField(child=serializers.CharField())


class AnswerTypeSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()
    description = serializers.CharField()
    category = serializers.CharField()
    icon = serializers.CharField()
    capabilities = CapabilitiesSerializer()


class AnswerTypeDetailSerializer(AnswerTypeSerializer):
    config_schema = ConfigFieldSerializer(many=True)
    default_config = serializers.DictField()
    example_value = serializers.JSONField(allow_null=True)
