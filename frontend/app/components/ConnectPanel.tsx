"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { AiReviewToggle } from "./AiReviewToggle";
import { useAiReviewConsent } from "../lib/useAiReviewConsent";
import {
  ConnectionTable,
  ConnectionTablesResponse,
  ConnectorCatalog,
  ConnectorField,
  ConnectorInfo,
  SavedConnection,
} from "../lib/types";

interface Props {
  loading: boolean;
  /** Called when the person opens a connection (so the page can point the
   * scan job at it). */
  onSelectConnection: (id: string | null) => void;
  onScan: (formData: FormData) => void;
}

async function readJson(res: Response) {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || body.error || `Request failed (HTTP ${res.status})`);
  return body;
}

function fmtRows(n: number | null) {
  return n == null ? null : `~${n.toLocaleString()} rows`;
}

function defaultsFor(c: ConnectorInfo): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of c.fields) if (f.default != null) out[f.key] = String(f.default);
  return out;
}

function visible(f: ConnectorField, values: Record<string, string>) {
  return Object.entries(f.show_if).every(([k, v]) => (values[k] ?? "") === v);
}

function usesOAuth(c: ConnectorInfo, values: Record<string, string>) {
  if (c.auth === "oauth") return true;
  if (c.auth === "mixed" && c.oauth_field) return (values[c.oauth_field[0]] ?? "") === c.oauth_field[1];
  return false;
}

const labelStyle = { fontSize: "0.75rem", color: "var(--text-muted)", fontWeight: 600 } as const;
const fieldStyle = { display: "flex", flexDirection: "column", gap: "0.25rem" } as const;

export function ConnectPanel({ loading, onSelectConnection, onScan }: Props) {
  const [catalog, setCatalog] = useState<ConnectorCatalog | null>(null);
  const [connectorKey, setConnectorKey] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [name, setName] = useState("");
  const [connections, setConnections] = useState<SavedConnection[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [info, setInfo] = useState<ConnectionTablesResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [instructions, setInstructions] = useState<Record<string, string>>({});
  const [aiReviewConsent, setAiReviewConsent] = useAiReviewConsent();
  const [rowLimit, setRowLimit] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const connector = useMemo(
    () => catalog?.connectors.find((c) => c.key === connectorKey) ?? null,
    [catalog, connectorKey]
  );
  const labelFor = useCallback(
    (key: string) => catalog?.connectors.find((c) => c.key === key)?.label ?? key,
    [catalog]
  );

  function pickConnector(c: ConnectorInfo) {
    if (c.status !== "ready") {
      setNote(`${c.label} is on the roadmap: same screen, same scan, no uploading.`);
      return;
    }
    setNote(null);
    setError(null);
    setConnectorKey(c.key);
    setValues(defaultsFor(c));
  }

  const loadTables = useCallback(
    async (id: string) => {
      setBusy(true);
      setError(null);
      try {
        const data: ConnectionTablesResponse = await readJson(await fetch(`/api/connections/${id}/tables`));
        setActiveId(id);
        onSelectConnection(id);
        setInfo(data);
        setSelected(new Set(data.tables.slice(0, 25).map((t) => t.id)));
        setInstructions({});
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        setInfo(null);
      } finally {
        setBusy(false);
      }
    },
    [onSelectConnection]
  );

  const refresh = useCallback(async () => {
    try {
      const data = await readJson(await fetch("/api/connections"));
      setConnections(data.connections ?? []);
    } catch {
      // The list is a convenience; the form still works without it.
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const cat: ConnectorCatalog = await readJson(await fetch("/api/connectors"));
        setCatalog(cat);
        const first = cat.connectors.find((c) => c.status === "ready");
        if (first) {
          setConnectorKey(first.key);
          setValues(defaultsFor(first));
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
      await refresh();
      // Coming back from a browser sign-in (e.g. Google): ?connection= or ?oauth_error=
      const params = new URLSearchParams(window.location.search);
      const returned = params.get("connection");
      const oauthError = params.get("oauth_error");
      if (returned || oauthError) {
        params.delete("connection");
        params.delete("oauth_error");
        const qs = params.toString();
        window.history.replaceState(null, "", window.location.pathname + (qs ? `?${qs}` : ""));
      }
      if (oauthError) setError(oauthError);
      if (returned) loadTables(returned);
    })();
  }, [refresh, loadTables]);

  async function submitConnect(e: FormEvent) {
    e.preventDefault();
    if (!connector) return;
    setBusy(true);
    setError(null);
    const settings: Record<string, string> = {};
    for (const f of connector.fields) {
      if (visible(f, values) && values[f.key] !== undefined && values[f.key] !== "") settings[f.key] = values[f.key];
    }
    try {
      if (usesOAuth(connector, values)) {
        const { authorization_url } = await readJson(
          await fetch(`/api/connectors/${connector.key}/oauth/start`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name || connector.label, settings }),
          })
        );
        window.location.assign(authorization_url);
        return;
      }
      const saved: SavedConnection = await readJson(
        await fetch("/api/connections", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ type: connector.key, name, settings }),
        })
      );
      // Secrets leave the browser's memory as soon as they're saved.
      setValues((prev) => {
        const next = { ...prev };
        for (const f of connector.fields) if (f.secret) delete next[f.key];
        return next;
      });
      await refresh();
      await loadTables(saved.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function disconnect(id: string) {
    await fetch(`/api/connections/${id}`, { method: "DELETE" }).catch(() => undefined);
    if (id === activeId) {
      setActiveId(null);
      setInfo(null);
      onSelectConnection(null);
    }
    refresh();
  }

  function toggle(tableId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(tableId)) next.delete(tableId);
      else next.add(tableId);
      return next;
    });
  }

  function scan() {
    if (!info || selected.size === 0) return;
    const chosen = info.tables.filter((t) => selected.has(t.id));
    const fd = new FormData();
    fd.append("tables", JSON.stringify(chosen.map((t) => t.id)));
    const ins: Record<string, string> = {};
    for (const t of chosen) if (instructions[t.id]?.trim()) ins[t.name] = instructions[t.id].trim();
    if (Object.keys(ins).length > 0) fd.append("custom_instructions", JSON.stringify(ins));
    fd.append("ai_review_consent", String(aiReviewConsent));
    if (rowLimit.trim()) fd.append("row_limit", rowLimit.trim());
    onScan(fd);
  }

  const card = { display: "flex", flexDirection: "column", gap: "1rem", padding: "1.5rem", maxWidth: "720px" } as const;
  const oauth = connector ? usesOAuth(connector, values) : false;

  function renderField(f: ConnectorField) {
    if (!visible(f, values)) return null;
    const value = values[f.key] ?? "";
    const set = (v: string) => setValues((prev) => ({ ...prev, [f.key]: v }));
    let input;
    if (f.kind === "select") {
      input = (
        <select className="input" value={value} onChange={(e) => set(e.target.value)}>
          {f.options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      );
    } else if (f.kind === "textarea") {
      input = (
        <textarea
          className="input"
          rows={5}
          value={value}
          placeholder={f.placeholder ?? undefined}
          onChange={(e) => set(e.target.value)}
          spellCheck={false}
          autoComplete="off"
          style={{ fontFamily: "ui-monospace, monospace", fontSize: "0.75rem" }}
        />
      );
    } else {
      input = (
        <input
          className="input"
          type={f.kind === "password" ? "password" : "text"}
          inputMode={f.kind === "number" ? "numeric" : undefined}
          value={value}
          placeholder={f.placeholder ?? undefined}
          autoComplete={f.secret ? "new-password" : "off"}
          onChange={(e) => set(f.kind === "number" ? e.target.value.replace(/\D/g, "") : e.target.value)}
        />
      );
    }
    return (
      <label key={f.key} style={fieldStyle}>
        <span style={labelStyle}>
          {f.label}
          {f.required || f.kind === "select" ? "" : " (optional)"}
        </span>
        {input}
        {f.help && <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>{f.help}</span>}
      </label>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      <form onSubmit={submitConnect} className="card fade-in" style={card}>
        <div>
          <h2 style={{ fontSize: "1.0625rem", fontWeight: 600, margin: "0 0 0.2rem", color: "var(--text-primary)" }}>
            1. Connect a data source
          </h2>
          <p style={{ fontSize: "0.8125rem", color: "var(--text-muted)", margin: 0 }}>
            Connect once; nothing is uploaded by hand. Access is read-only. Passwords, keys and sign-in tokens
            are kept encrypted on the server only, never shown again and never sent to any AI.
          </p>
        </div>

        <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
          {(catalog?.connectors ?? []).map((c) => {
            const active = c.key === connectorKey;
            const soon = c.status !== "ready";
            return (
              <button
                key={c.key}
                type="button"
                onClick={() => pickConnector(c)}
                title={c.description}
                style={{
                  all: "unset",
                  cursor: "pointer",
                  padding: "0.45rem 0.8rem",
                  borderRadius: "var(--radius-sm)",
                  fontSize: "0.8125rem",
                  fontWeight: 600,
                  border: `1px solid ${active ? "var(--accent)" : "var(--gridline)"}`,
                  background: active ? "color-mix(in srgb, var(--accent) 12%, var(--surface-1))" : "var(--surface-2)",
                  color: soon ? "var(--text-muted)" : "var(--text-primary)",
                }}
              >
                {c.label}
                {soon && <span style={{ fontWeight: 400 }}> · soon</span>}
              </button>
            );
          })}
        </div>
        {note && <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", margin: 0 }}>{note}</p>}
        {connector && (
          <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", margin: 0 }}>{connector.description}</p>
        )}

        {connector && connector.fields.map(renderField)}

        <label style={fieldStyle}>
          <span style={labelStyle}>Connection name (optional)</span>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. CRM database" />
        </label>

        {connector && oauth && !connector.oauth_configured && (
          <p style={{ fontSize: "0.8125rem", color: "var(--status-warning)", margin: 0 }}>
            Browser sign-in for {connector.label} isn&apos;t set up on this server yet (the admin adds a Google
            Cloud OAuth client). A service-account key works without it.
          </p>
        )}

        {error && (
          <div
            role="alert"
            style={{
              padding: "0.65rem 0.9rem",
              borderRadius: "var(--radius-sm)",
              border: "1px solid var(--status-critical)",
              background: "var(--surface-1)",
              fontSize: "0.875rem",
              color: "var(--text-primary)",
            }}
          >
            {error}
          </div>
        )}

        <button
          type="submit"
          className="btn-primary"
          disabled={busy || loading || !connector || (oauth && !connector.oauth_configured)}
          style={{ padding: "0.65rem 1rem" }}
        >
          {busy ? "Connecting..." : oauth ? `Sign in to ${connector?.label ?? ""}` : "Test and connect"}
        </button>

        {connections.length > 0 && (
          <div style={{ borderTop: "1px solid var(--gridline)", paddingTop: "0.9rem" }}>
            <div style={{ ...labelStyle, marginBottom: "0.4rem" }}>Saved connections</div>
            {connections.map((c) => (
              <div
                key={c.id}
                style={{ display: "flex", alignItems: "center", gap: "0.75rem", padding: "0.3rem 0", fontSize: "0.875rem" }}
              >
                <span style={{ flex: 1, color: c.id === activeId ? "var(--accent)" : "var(--text-primary)", fontWeight: 600 }}>
                  {c.name}
                  <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>
                    {" "}
                    · {labelFor(c.type)}
                    {c.account ? ` · ${c.account}` : ""}
                  </span>
                </span>
                <button
                  type="button"
                  onClick={() => loadTables(c.id)}
                  style={{ all: "unset", cursor: "pointer", color: "var(--accent)", fontSize: "0.8125rem" }}
                >
                  Open
                </button>
                <button
                  type="button"
                  onClick={() => disconnect(c.id)}
                  style={{ all: "unset", cursor: "pointer", color: "var(--text-muted)", fontSize: "0.8125rem" }}
                >
                  Disconnect
                </button>
              </div>
            ))}
          </div>
        )}
      </form>

      {info && (
        <div className="card fade-in" style={card}>
          <div>
            <h2 style={{ fontSize: "1.0625rem", fontWeight: 600, margin: "0 0 0.2rem", color: "var(--text-primary)" }}>
              2. Choose tables to score
            </h2>
            <p style={{ fontSize: "0.8125rem", color: "var(--text-muted)", margin: 0 }}>
              Connected to <strong>{info.connection.name}</strong>
              {info.connection.account ? ` as ${info.connection.account}` : ""}. Large tables are sampled: the first{" "}
              {Number(info.row_limit).toLocaleString()} rows of each.
            </p>
          </div>

          {info.tables.length === 0 && (
            <p style={{ fontSize: "0.875rem", color: "var(--text-secondary)", margin: 0 }}>
              No tables found here. For Google Drive, check the folder link and that the files are CSV, Google
              Sheets or Excel.
            </p>
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
            {info.tables.map((t: ConnectionTable) => {
              const on = selected.has(t.id);
              const meta = [
                fmtRows(t.approx_rows),
                t.columns.length > 0 ? `${t.columns.length} columns` : null,
                t.primary_key.length > 0 ? `key: ${t.primary_key.join(", ")}` : null,
                t.description,
              ].filter(Boolean);
              return (
                <div
                  key={t.id}
                  style={{
                    border: "1px solid var(--gridline)",
                    borderRadius: "var(--radius-sm)",
                    padding: "0.75rem 0.875rem",
                    background: "var(--surface-2)",
                    display: "flex",
                    flexDirection: "column",
                    gap: "0.5rem",
                  }}
                >
                  <label style={{ display: "flex", alignItems: "center", gap: "0.6rem", cursor: "pointer", flexWrap: "wrap" }}>
                    <input type="checkbox" checked={on} onChange={() => toggle(t.id)} />
                    <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>{t.name}</span>
                    <span className="tabular" style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
                      {meta.join(" · ")}
                    </span>
                  </label>
                  {t.foreign_keys.length > 0 && (
                    <div style={{ fontSize: "0.75rem", color: "var(--text-secondary)", paddingLeft: "1.6rem" }}>
                      {t.foreign_keys.map((f, i) => (
                        <div key={i}>
                          {f.columns.join(", ")} → {f.references}
                        </div>
                      ))}
                    </div>
                  )}
                  {on && (
                    <input
                      type="text"
                      className="input"
                      style={{ fontSize: "0.8125rem" }}
                      placeholder="Optional instruction for this table only (plain English)"
                      value={instructions[t.id] ?? ""}
                      onChange={(e) => setInstructions((prev) => ({ ...prev, [t.id]: e.target.value }))}
                    />
                  )}
                </div>
              );
            })}
          </div>

          <label style={{ ...fieldStyle, maxWidth: "260px" }}>
            <span style={labelStyle}>Max rows per table (optional)</span>
            <input
              className="input"
              inputMode="numeric"
              placeholder={String(info.row_limit)}
              value={rowLimit}
              onChange={(e) => setRowLimit(e.target.value.replace(/\D/g, ""))}
            />
          </label>

          <AiReviewToggle checked={aiReviewConsent} onChange={setAiReviewConsent} />

          <button
            type="button"
            className="btn-primary"
            onClick={scan}
            disabled={loading || selected.size === 0 || selected.size > 25}
            style={{ padding: "0.65rem 1rem" }}
          >
            {loading
              ? "Scanning..."
              : selected.size > 25
                ? "Pick at most 25 tables"
                : `Score ${selected.size} table${selected.size === 1 ? "" : "s"}`}
          </button>
        </div>
      )}
    </div>
  );
}
