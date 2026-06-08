# Argus Design System

> The design system for **Argus** — a universal application testing tool that can test **any** software regardless of interface: desktop GUI apps (Win32, Qt, GTK, Electron), CLI/terminal tools, web browsers and TUIs. Argus uses multimodal LLMs to observe, reason about, and validate behavior, and is fully provider-agnostic.

This repository is the single source of truth for how Argus looks, reads, and feels. It contains design tokens, a webfont stack, a logo, a React component library, foundation specimen cards, and high-fidelity UI kits for all three Argus surfaces.

---

## The product, in one paragraph

Argus watches an application the way a person would — through **screenshots + vision**, the **OS accessibility tree**, **terminal output**, and the **browser DOM** — then drives it to satisfy tests written as a mix of **natural-language steps** and **structured assertions** (YAML in a `.argus/` directory). Execution is **hybrid-agentic**: the LLM fills in the ambiguity of an NL step, but user-defined assertions are always authoritative. It ships as **three surfaces from one installer** — a **CLI** (`argus run/watch/init/record/report/serve`), a **desktop app** (Tauri · Rust + React), and a **self-hosted web dashboard** — and is **provider-agnostic** (Ollama, OpenAI, Anthropic, Azure, Gemini, LiteLLM).

### Sources used to build this system
- **GitHub:** `Varun-SV/Argus` — https://github.com/Varun-SV/Argus  · At build time the repository contained only an MIT `LICENSE` (no product code, design assets, or README), so **the brand below is an original direction** derived from the product concept and a detailed product spec. Explore the repo further as it grows to refine these designs against real code.
- **Product spec:** the architecture/feature brief for Argus (three surfaces, adapter system, provider matrix, YAML test format, reporting modes). Captured throughout this guide.

> ⚠️ **Caveat — please confirm.** Because no brand existed, the name *Argus* (Argus Panoptes, the hundred-eyed all-seeing watchman) drove an original "instrument / machine-vision" direction. Treat colors, logo, and type as a **first proposal** to react to, not a locked brand.

---

## Brand idea

**Argus is an instrument that sees everything.** The design language is that of a precise, dark-first observability tool — calm ink surfaces, a single vivid **Signal** color for action and live state, an amber **Beacon** for highlights, and an uncompromising **test-status** vocabulary (pass / fail / error / running / skipped / flaky). Monospace is a first-class citizen because the product is full of selectors, durations, test IDs, YAML and logs.

---

## CONTENT FUNDAMENTALS

How Argus writes. The voice is that of a **precise, trustworthy instrument** — calm, technical, never hype.

- **Person & address.** Speak to the developer as **you**; the product is **Argus** (third person, never "we" in UI). Imperatives for actions: "Run test", "Add provider", "Re-run".
- **Tone.** Direct, confident, lab-grade. Short declaratives. We state what happened, not how we feel about it: *"6 passed · 1 failed · 10.7s"*, *"Promo code is not valid for this region."* No exclamation marks, no congratulation ("Great job!"), no anxiety ("Uh oh!").
- **Casing.** Sentence case for everything — buttons, headings, menus ("Run all", "Pass / fail trend", not "Run All"). UPPERCASE is reserved for tiny mono eyebrows/labels (`ADAPTERS`, `STATUS`) and status badges (`PASSED`, `FAILED`).
- **Numbers & units.** Always concrete and monospace: durations as `1.84s` / `10.7s`, counts as `6/9 steps`, exit codes as `exit 1`, percentages as `94.2%`. Deltas carry a sign: `+1.8 pts`, `−0.6s`.
- **Domain nouns (use consistently).** *test*, *suite*, *step*, *assertion*, *run*, *adapter*, *provider*, *target*, *observation*, *flaky*, *report*. A test has *steps* and *assertions*; a *run* is one execution; an *adapter* connects to a *target*.
- **Identifiers.** File-style names stay literal and mono: `checkout.test.yaml`, `.argus/config.yaml`, `claude-3-5-sonnet`, `localhost:11434`.
- **Emoji.** **None.** Status is communicated by color + glyph (`✓ ✗ ⊘`) and the dot/badge system, never emoji.
- **Errors & failures.** Factual and actionable. Show the *expected* vs *actual*, the failing assertion, the exit code. Never blame the user; never anthropomorphize the model beyond a neutral "model reasoning" note.
- **Microcopy examples.** Empty: *"No runs yet — `argus run` to get started."* · Running: *"Running checkout.test.yaml…"* · Tooltip: *"Re-run this test ⌘R"*.

---

## VISUAL FOUNDATIONS

Answers to "what does Argus look like, exactly."

### Color & vibe
- **Dark-first.** The default theme is a cool, near-black **ink** (slate) ramp (`--slate-950` app background → `--slate-50` text). A light theme (`.theme-light`) exists for print/light dashboards but the product lives in the dark.
- **Brand = "Signal"** (`--signal-500 #14c9ac`), a teal-cyan scanner color. Used **sparingly**: primary buttons, active nav, focus rings, links, live/running state. Deliberately *not* the dev-tool-default blue/purple.
- **Accent = "Beacon"** (`--beacon-500 #ffae1a`), warm amber — the watchman's lantern. Highlights, the active-frame label, string literals in code.
- **Test status is sacred** and each hue is distinct so a glance reads true: pass = green `#2fc46c`, fail = red `#f5494f`, error = orange `#f97a33`, running = signal teal, skipped = slate, flaky = violet `#9a78f0`. Every status has a tinted background variant (`--status-*-tint`).
- **Imagery vibe.** Screenshots/"captured frames" are shown cool and crisp inside dark chrome with a thin reticle overlay and a brand/status ring around the element under observation. No warm filters, no grain. Where real imagery is absent we use abstract wireframe placeholders, never fabricated product UIs.

### Type
- **Display — Space Grotesk** (600/700, tight tracking): headlines, hero numbers, the wordmark.
- **UI / body — IBM Plex Sans** (400–700): the entire interface, labels, tables. 14px default.
- **Mono — IBM Plex Mono** (400–600): selectors, durations, IDs, YAML, logs, env vars, and all "machine" values. `zero 1` slashed-zero on.
- Engineering-credible, distinctive, and free. See `tokens/typography.css` and the Type cards.

### Space, radius, elevation
- **4px grid**, dense by default (`--space-*`). Control heights: 24/30/36/44/52 (`--control-*`). Fixed rails: 260px sidebar, 340px inspector, 52px topbar, 30px statusbar.
- **Tight technical corners:** `--radius-md 7px` for controls, `--radius-lg 10px` for cards/panels, pill for chips/badges. Nothing is very round.
- **Borders carry structure.** Most separation is a 1px hairline (`--border-subtle/-default/-strong` = translucent white over ink), not shadow. Inputs and panels read as etched, not floating.
- **Shadows are deep & cool** for dark surfaces (`--shadow-sm…xl`), used only for true overlays (menus, dialogs, the terminal/frame windows). Plus **glow** elevations (`--glow-signal/-beacon`) for live/running emphasis and the focus ring.

### Motion
- **Crisp, instrument-like, no bounce.** Durations 80–480ms (`--dur-*`), `--ease-standard` for most UI, `--ease-out` for enters, `--ease-linear` for spinners/scan/progress.
- Motion **communicates state**, never decorates: the running status dot pulses, the running progress bar shimmers, the terminal streams line-by-line, the focus ring snaps in. No infinite decorative loops on content.
- **Hover** = subtle surface lift (`--bg-hover`, border → stronger) or, on brand buttons, a lighter brand shade. **Press** = brand-active (darker) + a tiny `scale(0.985)` + 0.5px nudge. Honors `prefers-reduced-motion`.

### Cards, inputs, transparency
- **Cards:** `--bg-surface` fill, 1px `--border-subtle`, `--radius-lg`, `--shadow-sm`; interactive cards lift 1px and strengthen their border on hover.
- **Inputs:** sunken (`--bg-inset`) with a hairline border; focus = brand border + 3px brand-tint halo; errors swap to the fail hue.
- **Transparency/blur** is used judiciously: overlay scrims (`--bg-overlay`), the frame caption chip (backdrop-blur), and translucent hover/border layers so the system re-tints cleanly between themes.

---

## ICONOGRAPHY

- **System: [Lucide](https://lucide.dev)** — 1.9px stroke, rounded caps/joins, 24px grid. It matches the precise, technical, line-based feel and has full coverage for a dev tool (play, scan-line, file-code-2, sparkles, terminal, git-branch, sliders, trending-up, …). **This is a substitution flagged for your confirmation** — Argus had no icon set; Lucide is the recommended default.
- **Delivery.** UI kits load Lucide from CDN (`lucide@0.460.0` UMD) and call `createIcons()`. In production, install the `lucide-react` package and keep stroke width at ~1.9.
- **Sizing.** 13–17px inline in dense UI; icons inherit `currentColor`. Status glyphs in the terminal use literal unicode (`✓ ✗ ⊘ ●`) colored by status.
- **The logo** is the one bespoke mark (a machine-vision **reticle** — viewfinder brackets + aperture ring + hexagonal pupil). It is *not* part of the icon set. See `assets/`.
- **Emoji / unicode:** emoji are never used. Unicode is used only for the terminal status glyphs and arrows (`→ ↳ ─`).

---

## What's in here (index / manifest)

### Root
- **`styles.css`** — the global entry point consumers link. An `@import` manifest only.
- **`readme.md`** — this guide. · **`SKILL.md`** — Agent-Skill front-matter for use in Claude Code.

### `tokens/` — foundations (all `@import`ed by `styles.css`)
| File | What |
|---|---|
| `colors.css` | ink ramp, Signal, Beacon, status hues, semantic aliases, syntax colors, light theme |
| `typography.css` | font families, weights, type scale, tracking, helper classes |
| `spacing.css` | 4px space scale, control heights, layout rails, z-index |
| `elevation.css` | radii, border widths, shadows, glow, focus ring |
| `motion.css` | durations, easing, transitions, keyframes |
| `fonts.css` | Google Fonts `@import` (Space Grotesk · IBM Plex Sans · IBM Plex Mono) |
| `base.css` | reset + app defaults (scrollbars, selection, focus) |

### `components/` — React primitives (compiled into the runtime library)
`buttons/` Button · IconButton  ·  `forms/` Input · Select · Switch · Checkbox  ·  `data/` **StatusBadge** · Badge · Tag  ·  `feedback/` Tooltip · ProgressBar  ·  `surface/` Card · Tabs.
Each has `<Name>.jsx`, `<Name>.d.ts`, `<Name>.prompt.md`, and a directory `*.card.html`. Styling lives in `components/components.css` (`.ds-*` classes, imported by `styles.css`) so the components are self-contained and consumers get the CSS for free.

### `cards/` — foundation specimen cards
Colors, type, spacing, radii, elevation, brand — rendered in the Design System tab.

### `ui_kits/` — full-screen product recreations
| Kit | Surface | Highlights |
|---|---|---|
| `console/` | **Desktop app** (Tauri) | Runs (test tree → run detail, captured-frame timeline, step inspector with assertion diff), YAML Editor, Providers config. The hero kit. |
| `dashboard/` | **Web dashboard** | KPIs, 14-day pass/fail trend, flakiness sparklines, run history. |
| `cli/` | **CLI / terminal** | Streamed `argus run` output with colored pass/fail + command rail. |

### `assets/`
`logo-mark.svg` (color) · `logo-mark-mono.svg` (currentColor) · `logo-lockup.svg` (mark + wordmark).

---

## Using the system

**Plain HTML / specimens:** link the one stylesheet and use the `.ds-*` classes + tokens:
```html
<link rel="stylesheet" href="styles.css">
<button class="ds-btn ds-btn--primary">Run test</button>
<span class="ds-status ds-status--pass"><span class="ds-status__dot"></span>Passed</span>
```

**React components:** the compiler bundles every `components/**/<Name>.jsx` into a runtime library exposed on a window namespace (run `check_design_system` for the exact name). In a Design-System-tab card, load the bundle and destructure:
```html
<script src="../../_ds_bundle.js"></script>
<script type="text/babel">
  const { Button, StatusBadge } = window.ArgusDesignSystem_xxxxxx;
</script>
```

**Fonts:** loaded via Google Fonts in `tokens/fonts.css`. No proprietary typeface exists — see the substitution note below.

---

## Known caveats / substitutions (please confirm)
1. **No existing brand.** Colors, the reticle logo, and the type pairing are an **original proposal**. Tell me what to keep or change.
2. **Fonts** are Google Fonts (Space Grotesk · IBM Plex Sans · IBM Plex Mono), loaded from CDN — no local font binaries are bundled. If Argus adopts a specific typeface, drop the files in and I'll wire `@font-face`.
3. **Icons** are **Lucide** (substituted). Swap for your set if you have one.
4. **UI kits** are visual recreations from the product spec (the repo had no UI code). They cut functional corners by design.
