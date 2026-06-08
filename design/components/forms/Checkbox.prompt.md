Labelled checkbox — use for multi-select lists and form opt-ins (which adapters, which reports).

```jsx
<Checkbox label="Emit JUnit XML" defaultChecked />
<Checkbox label="Capture video" checked={video} onChange={e => setVideo(e.target.checked)} />
```

- Controlled via **checked**/**onChange**, or uncontrolled via **defaultChecked**.
- Use Checkbox (not Switch) inside forms that are submitted as a batch.
