/* Argus CLI — terminal transcript for `argus run` (streamed) */
(function () {
  // type: cmd | banner | info | step | assert | note | diff | blank | rule | summary
  const TRANSCRIPT = [
    { type: "cmd", text: "argus run .argus/checkout.test.yaml --adapter browser" },
    { type: "banner", text: "◎ Argus v0.4.1 · hybrid-agentic · provider: claude-3-5-sonnet (vision)" },
    { type: "info", text: "launching browser · https://shop.example.com" },
    { type: "info", text: "observing via screenshot + a11y tree + DOM (CDP)" },
    { type: "blank" },
    { type: "step", status: "pass", n: "setup", text: "Launch browser · dismiss cookie banner", dur: "0.62s" },
    { type: "step", status: "pass", n: "1", text: "Log in with test credentials", dur: "1.84s" },
    { type: "note", text: 'typed test credentials → clicked "Sign in"' },
    { type: "step", status: "pass", n: "2", text: "Add the wireless headphones to the cart", dur: "2.10s" },
    { type: "step", status: "pass", n: "3", text: "Open the cart and proceed to checkout", dur: "1.27s" },
    { type: "assert", status: "pass", text: 'visible "Order summary"', dur: "0.18s" },
    { type: "step", status: "pass", n: "5", text: "Enter shipping details for the test address", dur: "2.46s" },
    { type: "step", status: "fail", n: "6", text: "Apply promo code SAVE10", dur: "1.93s" },
    { type: "note", text: 'entered "SAVE10" → clicked "Apply"' },
    { type: "assert", status: "fail", text: 'text_contains "$10.00 off"', dur: "0.21s" },
    { type: "diff", expected: '"$10.00 off"', actual: '"Promo code is not valid for this region."' },
    { type: "step", status: "skip", n: "td", text: "Close browser · capture trace", dur: "skipped" },
    { type: "blank" },
    { type: "rule", text: "checkout.test.yaml" },
    { type: "summary", pass: 6, fail: 1, skip: 1, dur: "10.7s" },
    { type: "report", text: ".argus/reports/4821.html" },
    { type: "exit", code: 1 },
    { type: "rule" },
  ];

  window.CLI_DATA = { TRANSCRIPT };
})();
