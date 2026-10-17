import type { ReactNode } from "react";
import { IconBlank, IconCopy, IconFormat, IconActivity, IconSpark, IconLink, IconLayers, IconShield } from "./Icons";

interface Item {
  icon: ReactNode;
  title: string;
  text: string;
}

const SINGLE: Item[] = [
  { icon: <IconBlank />, title: "Completeness", text: "Blank or missing values, including placeholders like N/A." },
  { icon: <IconCopy />, title: "Uniqueness", text: "Duplicate records, even when formatted differently." },
  { icon: <IconFormat />, title: "Validity", text: "Values in the wrong format or of the wrong type." },
  { icon: <IconActivity />, title: "Consistency", text: "Statistical outliers that don't fit their column." },
  { icon: <IconSpark />, title: "Accuracy", text: "AI judgment of whether rows make sense. Only when you opt in." },
];

const SCHEMA: Item[] = [
  { icon: <IconLayers />, title: "A score per table", text: "Each uploaded table gets its own full quality scorecard." },
  { icon: <IconLink />, title: "Foreign keys", text: "Finds keys that don't resolve against any other uploaded table." },
  { icon: <IconSpark />, title: "Cross-table content", text: "With AI review on, checks a child row against the parent it points to." },
  { icon: <IconShield />, title: "Quarantine", text: "Rows carrying prompt-injection text are kept away from the AI." },
];

export function AboutPanel({ kind }: { kind: "single" | "schema" }) {
  const items = kind === "single" ? SINGLE : SCHEMA;
  return (
    <aside className="card about-panel fade-in" aria-label="What gets checked">
      <h3 className="about-title">{kind === "single" ? "What gets scored" : "What a schema scan adds"}</h3>
      {items.map((it) => (
        <div key={it.title} className="about-item">
          <span className="about-icon">{it.icon}</span>
          <div>
            <h4>{it.title}</h4>
            <p>{it.text}</p>
          </div>
        </div>
      ))}
    </aside>
  );
}
