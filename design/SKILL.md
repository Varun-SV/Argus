---
name: argus-design
description: Use this skill to generate well-branded interfaces and assets for Argus, either for production or throwaway prototypes/mocks/etc. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping.
user-invocable: true
---

Read the `readme.md` file within this skill, and explore the other available files.

If creating visual artifacts (slides, mocks, throwaway prototypes, etc), copy assets out and create static HTML files for the user to view. If working on production code, you can copy assets and read the rules here to become an expert in designing with this brand.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions, and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.

## Quick map
- `readme.md` — the full design guide: brand idea, content voice, visual foundations, iconography, manifest. **Start here.**
- `styles.css` — the single global stylesheet to link. Pulls in all tokens + component CSS.
- `tokens/` — colors, typography, spacing, elevation, motion, fonts, base reset (CSS custom properties).
- `components/` — React primitives (Button, IconButton, Input, Select, Switch, Checkbox, StatusBadge, Badge, Tag, Tooltip, ProgressBar, Card, Tabs). Styling is class-based (`.ds-*`) in `components/components.css`.
- `cards/` — foundation specimen cards (color/type/spacing/brand).
- `ui_kits/` — full-screen recreations: `console/` (desktop app), `dashboard/` (web), `cli/` (terminal).
- `assets/` — logo (mark, mono, lockup).

## Fastest path to an on-brand mock
1. Link `styles.css`.
2. Use tokens (`var(--brand)`, `var(--bg-surface)`, `var(--status-pass)`, `var(--font-mono)` …) and `.ds-*` component classes.
3. Icons: Lucide (`lucide@0.460.0` UMD, `createIcons()`), ~1.9px stroke.
4. Dark-first. Signal teal for action/live state; Beacon amber for highlights. Monospace for all machine values. No emoji.
5. Lift patterns (sidebars, run detail, terminal, tables, charts) from `ui_kits/`.
