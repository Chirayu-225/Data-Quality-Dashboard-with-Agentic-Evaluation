import type { ReactNode } from "react";
import { IconShield, IconSpark, IconLock, IconTable } from "./Icons";
import { HeroPreview } from "./HeroPreview";

const CHIPS = [
  { icon: <IconTable size={14} />, label: "Deterministic checks" },
  { icon: <IconSpark size={14} />, label: "Opt-in AI review" },
  { icon: <IconLock size={14} />, label: "PII masked before AI" },
  { icon: <IconShield size={14} />, label: "Prompt-injection quarantine" },
];

export function PageHero({
  eyebrow,
  title,
  accent,
  subtitle,
  note,
}: {
  eyebrow: string;
  title: string;
  accent: string;
  subtitle: string;
  note?: ReactNode;
}) {
  return (
    <header className="page-hero fade-in">
      <div className="hero-grid">
        <div>
          <span className="eyebrow">{eyebrow}</span>
          <h1 className="hero-title">
            {title} <span className="grad-text">{accent}</span>
          </h1>
          <p className="hero-sub">{subtitle}</p>
          <div className="chip-row">
            {CHIPS.map((c) => (
              <span key={c.label} className="chip">
                {c.icon}
                {c.label}
              </span>
            ))}
          </div>
        </div>
        <HeroPreview />
      </div>
      {note && <p className="hero-note">{note}</p>}
    </header>
  );
}
