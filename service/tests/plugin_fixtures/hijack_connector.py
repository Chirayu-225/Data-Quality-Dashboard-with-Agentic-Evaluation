"""Tries to replace the installed MySQL connector. Must be ignored."""
from app.connectors.registry import ConnectorSpec, FieldSpec
from tests.plugin_fixtures.memory_connector import MemorySource

CONNECTOR_SPECS = [ConnectorSpec(
    key="mysql", label="Evil MySQL", category="database", description="hijack",
    fields=(FieldSpec("host", "Host"),), factory=MemorySource,
)]
