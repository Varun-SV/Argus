# Argus

**Argus** is a universal application testing tool that can test *any* software regardless of interface: desktop GUI apps (Win32, Qt, GTK, Electron), CLI/terminal tools, web browsers, and TUIs. It uses multimodal LLMs to observe, reason about, and validate behavior, and is fully provider-agnostic.

> Argus watches an application the way a person would — through **screenshots + vision**, the **OS accessibility tree**, **terminal output**, and the **browser DOM** — then drives it to satisfy tests written as a mix of natural-language steps and structured assertions (YAML in a `.argus/` directory).

---

## Three surfaces

| Surface | Description |
|---|---|
| **CLI** | `argus run/watch/init/record/report/serve` — the primary developer interface and CI integration |
| **Desktop app** | Tauri (Rust + React) — run detail with captured-frame timeline, YAML editor, provider config |
| **Web dashboard** | Self-hosted (`argus serve`) — KPIs, pass/fail trend, flakiness tracking, run history |

---

## Test format

Tests live in `.argus/` as YAML files combining natural-language steps with structured assertions:

```yaml
name: Checkout with promo code
target:
  adapter: browser
  launch: "https://shop.example.com"

steps:
  - "Log in with test credentials"
  - "Add the wireless headphones to the cart"
  - assert:
      visible: "Order summary"
  - "Apply promo code SAVE10"
  - assert:
      text_contains: "$10.00 off"
```

---

## Provider support

Argus is provider-agnostic: Anthropic, OpenAI, Ollama, Google Gemini, Azure OpenAI, and LiteLLM (proxy). Configure in `.argus/config.yaml` or via `ARGUS_PROVIDER` / `ARGUS_API_KEY` env vars.

---

## Design system

The `design/` directory contains the full Argus design system — tokens, React component library, and high-fidelity UI kits for all three surfaces.

- [`design/readme.md`](design/readme.md) — design guide: brand, color, type, motion, iconography
- [`design/styles.css`](design/styles.css) — single entry-point stylesheet (import this)
- [`design/tokens/`](design/tokens/) — CSS custom properties (colors, type, spacing, elevation, motion)
- [`design/components/`](design/components/) — React primitives (Button, Input, StatusBadge, etc.)
- [`design/assets/`](design/assets/) — logo mark, mono mark, lockup SVG
- [`design/ui_kits/console/`](design/ui_kits/console/index.html) — desktop app UI kit
- [`design/ui_kits/dashboard/`](design/ui_kits/dashboard/index.html) — web dashboard UI kit
- [`design/ui_kits/cli/`](design/ui_kits/cli/index.html) — CLI terminal UI kit

Open [`index.html`](index.html) for an interactive overview of all surfaces.

---

## License

MIT — see [LICENSE](LICENSE).
