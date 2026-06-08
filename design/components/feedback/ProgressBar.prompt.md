Progress / distribution bar — run progress, suite pass-rate, coverage.

```jsx
<ProgressBar value={72} />
<ProgressBar running />                  {/* indeterminate shimmer */}
<ProgressBar segments={[
  {kind:'pass', value:118}, {kind:'fail', value:6},
  {kind:'error', value:1}, {kind:'skip', value:3},
]} />
```

- Pass **value** (0–100) for a single bar, **running** for indeterminate, or **segments** for a stacked pass/fail/error/skip distribution.
- **thin** for inline 4px bars inside rows.
