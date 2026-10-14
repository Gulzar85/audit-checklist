"""API v1 serializers."""

from __future__ import annotations

from rest_framework import serializers


class HealthSerializer(serializers.Serializer):
    """Represents the payload returned by the API health endpoint."""

    status = serializers.CharField()
    version = serializers.CharField()
    environment = serializers.CharField()
    checks = serializers.DictField()


class APIRootSerializer(serializers.Serializer):
    """Represents the API v1 discovery payload."""

    version = serializers.CharField()
    endpoints = serializers.DictField(child=serializers.CharField())
