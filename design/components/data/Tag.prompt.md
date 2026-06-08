Monospace chip — use for adapters, providers, environments, and filter tokens.

```jsx
<Tag dotColor="var(--signal-500)">browser</Tag>
<Tag icon={<BoltIcon/>}>gpt-4o</Tag>
<Tag interactive onRemove={() => removeFilter('env:staging')}>env:staging</Tag>
```

- **dotColor** adds a leading status dot; **icon** renders a small glyph.
- **onRemove** shows a "×" (filter tokens); **interactive** adds hover feedback.
