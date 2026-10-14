"""API pagination."""

from __future__ import annotations

from rest_framework.pagination import PageNumberPagination


class StandardResultsSetPagination(PageNumberPagination):
    """Page-number pagination with a client-tunable page size."""

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
