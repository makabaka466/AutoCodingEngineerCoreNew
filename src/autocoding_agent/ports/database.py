"""Shared read-only database access port."""

from typing import Protocol

from autocoding_agent.database_models import DataQuery, QueryResult


class DatabaseReader(Protocol):
    def describe_schema(self) -> str:
        """Return bounded schema metadata without reading business rows."""

        ...

    def execute(self, query: DataQuery) -> QueryResult:
        """Execute one bounded read-only query."""

        ...
