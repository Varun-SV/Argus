The canonical test-outcome indicator — use it everywhere a run, step, or assertion result appears.

```jsx
<StatusBadge status="pass" />
<StatusBadge status="fail" />
<StatusBadge status="running" />        {/* pulsing dot */}
<StatusBadge status="flaky" label="Flaky · 2/5" />
<StatusBadge status="pass" solid />     {/* summary header emphasis */}
```

- **status**: `pass | fail | error | running | skipped | flaky`. `running` animates its dot.
- Default labels are "Passed / Failed / Error / Running / Skipped / Flaky" — override with **label**.
- Mono, uppercase, tinted by default; **solid** fills it for summary banners.
