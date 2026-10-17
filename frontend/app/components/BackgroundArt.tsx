/** Ambient backdrop: soft colour glows, a fading dot grid, and two quiet
 * illustrations in the side gutters on wide screens — a data table being
 * scanned (left) and a linked-schema graph (right). Purely decorative:
 * aria-hidden, no interaction, low contrast, and the motion stops under
 * prefers-reduced-motion. All geometry is fixed (no randomness) so server
 * and client render identically. */

// Deterministic skeleton bar widths for the table illustration.
const W = [62, 40, 74, 52, 36, 68, 46, 58, 70, 34, 56, 64, 42, 72, 48, 60];
const HOT: Record<number, string> = { 3: "var(--accent)", 7: "var(--status-warning)", 11: "var(--status-critical)", 14: "var(--accent)" };

function TableArt() {
  const rows = Array.from({ length: 15 }, (_, i) => i);
  return (
    <svg viewBox="0 0 300 600" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="beam" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--accent)" stopOpacity="0" />
          <stop offset="0.85" stopColor="var(--accent)" stopOpacity="0.28" />
          <stop offset="1" stopColor="var(--accent)" stopOpacity="0.9" />
        </linearGradient>
      </defs>
      {/* header */}
      <rect x="14" y="14" width="272" height="30" rx="8" fill="currentColor" opacity="0.07" />
      {[28, 112, 200].map((x, i) => (
        <rect key={x} x={x} y="25" width={[44, 52, 40][i]} height="8" rx="4" fill="currentColor" opacity="0.38" />
      ))}
      {/* rows */}
      {rows.map((i) => {
        const y = 62 + i * 36;
        const hot = HOT[i];
        return (
          <g key={i}>
            <rect x="14" y={y - 8} width="272" height="30" rx="8" fill="currentColor" opacity={i % 2 ? 0.035 : 0} />
            <rect x="28" y={y + 1} width={W[i] * 0.62} height="8" rx="4" fill="currentColor" opacity="0.2" />
            <rect x="112" y={y + 1} width={W[(i + 5) % 16] * 0.7} height="8" rx="4" fill="currentColor" opacity="0.2" />
            <rect
              x="200"
              y={y + 1}
              width={W[(i + 9) % 16] * 0.55}
              height="8"
              rx="4"
              fill={hot ?? "currentColor"}
              opacity={hot ? 0.6 : 0.2}
            />
            {hot && <circle cx="272" cy={y + 5} r="4" fill={hot} opacity="0.75" />}
          </g>
        );
      })}
      {/* scanning beam */}
      <g className="art-beam">
        <rect x="8" y="40" width="284" height="62" rx="6" fill="url(#beam)" />
      </g>
    </svg>
  );
}

const NODES: [number, number, number, string?][] = [
  [150, 60, 9, "var(--accent)"],
  [60, 130, 6],
  [236, 120, 7, "var(--brand-2)"],
  [110, 215, 8],
  [200, 245, 6],
  [40, 300, 7, "var(--brand-3)"],
  [150, 330, 9, "var(--accent)"],
  [250, 360, 6],
  [90, 420, 6],
  [190, 455, 8, "var(--brand-2)"],
  [60, 520, 6],
  [240, 540, 7],
];
const EDGES: [number, number][] = [
  [0, 1], [0, 2], [1, 3], [2, 4], [3, 4], [3, 5], [4, 6], [5, 6], [6, 7], [6, 8], [8, 9], [7, 9], [9, 10], [9, 11],
];

function GraphArt() {
  return (
    <svg viewBox="0 0 300 600" fill="none" aria-hidden="true">
      <g className="art-drift">
        {EDGES.map(([a, b], i) => (
          <line
            key={i}
            x1={NODES[a][0]}
            y1={NODES[a][1]}
            x2={NODES[b][0]}
            y2={NODES[b][1]}
            stroke="currentColor"
            strokeOpacity="0.2"
            strokeWidth="1.4"
            strokeDasharray={i % 3 === 0 ? "5 7" : undefined}
            className={i % 3 === 0 ? "art-flow" : undefined}
          />
        ))}
        {NODES.map(([x, y, r, c], i) => (
          <g key={i}>
            {c && <circle cx={x} cy={y} r={r + 9} fill={c} opacity="0.12" />}
            <circle cx={x} cy={y} r={r} fill={c ?? "currentColor"} opacity={c ? 0.75 : 0.28} />
            <circle cx={x} cy={y} r={r + 4} stroke="currentColor" strokeOpacity="0.14" strokeWidth="1" />
          </g>
        ))}
      </g>
    </svg>
  );
}

export function BackgroundArt() {
  return (
    <div className="bg-scene" aria-hidden="true">
      <div className="bg-orb o1" />
      <div className="bg-orb o2" />
      <div className="bg-orb o3" />
      <div className="bg-dots" />
      <div className="bg-side left">
        <TableArt />
      </div>
      <div className="bg-side right">
        <GraphArt />
      </div>
    </div>
  );
}
