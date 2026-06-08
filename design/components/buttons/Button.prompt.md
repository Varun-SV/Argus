Primary action control — use for the main action in any view; pair `variant="primary"` with secondary/ghost for hierarchy.

```jsx
<Button variant="primary" leftIcon={<PlayIcon />} onClick={runTest}>
  Run test
</Button>
<Button variant="secondary">Edit YAML</Button>
<Button variant="ghost" size="sm">Cancel</Button>
<Button variant="danger" loading>Deleting…</Button>
```

- **variant**: `primary` (brand fill, one per view), `secondary` (raised outline), `ghost` (toolbar/low-emphasis), `danger` (destructive, red).
- **size**: `sm` (30px), `md` (36px, default), `lg` (44px).
- **loading** swaps the label for a spinner and disables interaction. **block** stretches full width.
