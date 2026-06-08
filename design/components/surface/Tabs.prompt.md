Horizontal tab navigation — switch between views within a surface.

```jsx
const [tab, setTab] = React.useState("steps");
<Tabs value={tab} onChange={setTab} items={[
  {id:"steps", label:"Steps", count:14},
  {id:"logs", label:"Logs"},
  {id:"yaml", label:"YAML"},
]} />

<Tabs variant="pills" value={env} onChange={setEnv} items={[
  {id:"all", label:"All"}, {id:"staging", label:"Staging"},
]} />
```

- **variant**: `underline` (default, section nav) or `pills` (segmented filter).
- Controlled: track the active id yourself and pass it as **value**.
