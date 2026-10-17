"""A minimal third-party-style connector, used to prove a NEW connector
plugs in through the registry alone (no API/frontend/other-connector edits)."""
from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.connectors.registry import ConnectorSpec, FieldSpec
from app.connectors.source import ConnectorError, ExtractedTable, TableInfo, TabularSource

DATA = {
    "accounts": [{"id": "1", "name": "Acme"}, {"id": "2", "name": "Globex"}],
    "contacts": [{"id": "1", "account_id": "1"}, {"id": "2", "account_id": "42"}],
}


class MemorySource(TabularSource):
    def check(self):
        if self.config.settings.get("api_key") != "good-key-SECRET":
            raise ConnectorError("The API key was rejected.")
        if self.config.settings.get("explode"):
            raise RuntimeError("a bug inside the connector")

    def list_tables(self):
        return [TableInfo(n, len(r), [], ["id"], id=f"mem:{n}") for n, r in DATA.items()]

    def extract_selected(self, table_ids, row_limit=None):
        out = []
        for tid in table_ids:
            name = tid.removeprefix("mem:")
            if name not in DATA:
                raise ConnectorError(f"unknown table {tid}")
            rows = DATA[name][: row_limit or None]
            cols = [ColumnSchema(name=c, type=ColumnType.STRING, nullable=False) for c in rows[0]]
            t = CanonicalTable(tenant_id=self.config.tenant_id, source_id=self.config.source_id,
                               table_name=name, columns=cols, rows=[dict(r) for r in rows])
            out.append(ExtractedTable(t, len(rows), len(DATA[name]), True, {},
                                      hit_row_cap=len(rows) < len(DATA[name])))
        return out


CONNECTOR_SPECS = [ConnectorSpec(
    key="memory_crm", label="Memory CRM", category="crm", description="test-only",
    fields=(FieldSpec("api_key", "API key", kind="password", secret=True, required=True),
            FieldSpec("explode", "Explode", kind="text")),
    factory=MemorySource,
)]
