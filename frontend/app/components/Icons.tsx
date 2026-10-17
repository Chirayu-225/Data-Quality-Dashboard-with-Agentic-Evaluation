import type { ReactNode } from "react";

/** Small stroke icon set (24px grid, currentColor) — one visual language
 * for every icon in the product, no icon library dependency. */
function Icon({ size = 16, children }: { size?: number; children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

type P = { size?: number };

export const IconTable = (p: P) => (
  <Icon {...p}><rect x="3" y="4" width="18" height="16" rx="2.5" /><path d="M3 10h18M9 4v16" /></Icon>
);
export const IconShield = (p: P) => (
  <Icon {...p}><path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6l7-3z" /><path d="M9 12l2 2 4-4" /></Icon>
);
export const IconSpark = (p: P) => (
  <Icon {...p}><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z" /><path d="M19 17l.7 1.8L21.5 19.5l-1.8.7L19 22l-.7-1.8-1.8-.7 1.8-.7L19 17z" /></Icon>
);
export const IconLock = (p: P) => (
  <Icon {...p}><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" /></Icon>
);
export const IconClock = (p: P) => (
  <Icon {...p}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></Icon>
);
export const IconLayers = (p: P) => (
  <Icon {...p}><path d="M12 3l9 5-9 5-9-5 9-5z" /><path d="M3 13l9 5 9-5" /></Icon>
);
export const IconLink = (p: P) => (
  <Icon {...p}><path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1" /><path d="M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1" /></Icon>
);
export const IconRows = (p: P) => (
  <Icon {...p}><path d="M4 6h16M4 12h16M4 18h10" /></Icon>
);
export const IconCopy = (p: P) => (
  <Icon {...p}><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 00-2-2H6a2 2 0 00-2 2v8a2 2 0 002 2h2" /></Icon>
);
export const IconFormat = (p: P) => (
  <Icon {...p}><path d="M5 7V5h14v2M12 5v14M9 19h6" /></Icon>
);
export const IconActivity = (p: P) => (
  <Icon {...p}><path d="M3 12h4l3-8 4 16 3-8h4" /></Icon>
);
export const IconBlank = (p: P) => (
  <Icon {...p}><rect x="4" y="4" width="16" height="16" rx="3" strokeDasharray="3 3" /></Icon>
);
