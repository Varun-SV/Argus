Icon-only control for toolbars, table-row actions, and dense panels — always pass `label` for accessibility + tooltip.

```jsx
<IconButton label="Re-run" onClick={rerun}><RefreshIcon /></IconButton>
<IconButton variant="solid" label="Filter"><FilterIcon /></IconButton>
<IconButton active label="Grid view"><GridIcon /></IconButton>
```

- **variant**: `ghost` (default) or `solid` (raised surface for standalone use).
- **active** tints it with the brand for toggled/selected tools.
- **size**: `sm` (30px) or `md` (36px). Icons auto-size (~17px).
