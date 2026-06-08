/* Argus Console — demo data (fake, for the UI kit recreation) */
(function () {
  const ADAPTERS = {
    browser:      { label: "browser",     color: "var(--signal-500)" },
    "desktop-gui":{ label: "desktop-gui", color: "var(--beacon-500)" },
    cli:          { label: "cli",         color: "var(--violet-500)" },
    tui:          { label: "tui",         color: "var(--blue-500)"  },
    electron:     { label: "electron",    color: "var(--green-500)" },
  };

  const PROVIDERS = [
    { id: "anthropic", name: "Anthropic", model: "claude-3-5-sonnet", status: "connected", note: "Vision · primary" },
    { id: "openai",    name: "OpenAI",    model: "gpt-4o",            status: "connected", note: "Vision" },
    { id: "ollama",    name: "Ollama",    model: "llava:13b",         status: "connected", note: "Local · localhost:11434" },
    { id: "gemini",    name: "Google",    model: "gemini-2.0-flash",  status: "idle",      note: "Vision" },
    { id: "azure",     name: "Azure",     model: "gpt-4o",            status: "idle",      note: "eastus2" },
    { id: "litellm",   name: "LiteLLM",   model: "proxy",             status: "idle",      note: "catch-all" },
  ];

  // The detailed run shown in the center stage
  const checkoutSteps = [
    { id: "s0", kind: "setup",    text: "Launch browser · https://shop.example.com", status: "pass", dur: "0.62s",
      action: "navigated to target URL, waited for network idle", frame: "01" },
    { id: "s1", kind: "step",     text: "Log in with test credentials", status: "pass", dur: "1.84s",
      action: "typed user 'qa@example.com', filled password field, clicked “Sign in”",
      reasoning: "Resolved the login form by its labelled fields; submit button matched by accessible name.", frame: "02" },
    { id: "s2", kind: "step",     text: "Add the wireless headphones to the cart", status: "pass", dur: "2.10s",
      action: "searched 'wireless headphones', opened first result, clicked “Add to cart”",
      reasoning: "Product grid had 12 results; selected the first matching card.", frame: "03" },
    { id: "s3", kind: "step",     text: "Open the cart and proceed to checkout", status: "pass", dur: "1.27s",
      action: "clicked cart icon, clicked “Checkout”", frame: "04" },
    { id: "s4", kind: "assert",   text: "visible: \"Order summary\"", status: "pass", dur: "0.18s",
      assertion: "visible", expected: "Order summary", frame: "05" },
    { id: "s5", kind: "step",     text: "Enter shipping details for the test address", status: "pass", dur: "2.46s",
      action: "filled name, address, city, ZIP from env fixtures", frame: "06" },
    { id: "s6", kind: "step",     text: "Apply promo code SAVE10", status: "fail", dur: "1.93s",
      action: "entered 'SAVE10' into promo field, clicked “Apply”",
      reasoning: "The applied discount did not appear in the order total.", frame: "07" },
    { id: "s7", kind: "assert",   text: "text_contains: \"$10.00 off\"", status: "fail", dur: "0.21s",
      assertion: "text_contains", expected: "$10.00 off", actual: "Promo code is not valid for this region.", frame: "07" },
    { id: "s8", kind: "teardown", text: "Close browser · capture trace", status: "skipped", dur: "—",
      action: "skipped after failure (continue_on_failure: false)", frame: "—" },
  ];

  const SUITES = [
    {
      name: "checkout",
      tests: [
        { id: "t1", name: "checkout.test.yaml", adapter: "browser", provider: "gemini-2.0-flash",
          status: "fail", dur: "10.7s", steps: checkoutSteps, passed: 6, total: 9, flaky: false, active: true },
        { id: "t2", name: "guest-checkout.test.yaml", adapter: "browser", provider: "gpt-4o",
          status: "pass", dur: "8.4s", passed: 7, total: 7, flaky: false },
        { id: "t3", name: "cart-persistence.test.yaml", adapter: "browser", provider: "claude-3-5-sonnet",
          status: "flaky", dur: "9.1s", passed: 6, total: 7, flaky: true },
      ],
    },
    {
      name: "desktop",
      tests: [
        { id: "t4", name: "settings-dialog.test.yaml", adapter: "desktop-gui", provider: "claude-3-5-sonnet",
          status: "pass", dur: "5.2s", passed: 5, total: 5 },
        { id: "t5", name: "file-export.test.yaml", adapter: "electron", provider: "gpt-4o",
          status: "pass", dur: "6.8s", passed: 8, total: 8 },
      ],
    },
    {
      name: "cli",
      tests: [
        { id: "t6", name: "init-wizard.test.yaml", adapter: "cli", provider: "llava:13b",
          status: "pass", dur: "1.1s", passed: 4, total: 4 },
        { id: "t7", name: "migrate.test.yaml", adapter: "tui", provider: "claude-3-5-sonnet",
          status: "running", dur: "—", passed: 2, total: 6 },
      ],
    },
  ];

  const YAML = `# .argus/checkout.test.yaml
name: Checkout with promo code
target:
  adapter: browser
  launch: "https://shop.example.com"

setup:
  - "Launch browser and dismiss cookie banner"

env:
  address: fixtures/test-address.json

steps:
  - "Log in with test credentials"
  - "Add the wireless headphones to the cart"
  - "Open the cart and proceed to checkout"
  - assert:
      visible: "Order summary"
  - "Enter shipping details for the test address"
  - "Apply promo code SAVE10"
  - assert:
      text_contains: "$10.00 off"

teardown:
  - "Close browser and capture trace"
`;

  window.ARGUS_DATA = { ADAPTERS, PROVIDERS, SUITES, YAML, checkoutSteps };
})();
