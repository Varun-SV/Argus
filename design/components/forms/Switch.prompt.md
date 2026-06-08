Boolean toggle — use for instant on/off settings (watch mode, headless, parallel).

```jsx
<Switch label="Watch mode" defaultChecked />
<Switch label="Headless" checked={headless} onChange={e => setHeadless(e.target.checked)} />
```

- Controlled via **checked**/**onChange**, or uncontrolled via **defaultChecked**.
- Prefer Switch over Checkbox when the change takes effect immediately.
