Labelled text field — the default way to capture single-line input (URLs, launch commands, selectors, API keys).

```jsx
<Input label="Launch URL" placeholder="https://app.example.com"
       leadingIcon={<GlobeIcon/>} mono />
<Input label="API key" type="password" required
       error="Key is required for OpenAI provider" />
```

- **leadingIcon** sits inside the field; **mono** switches to IBM Plex Mono for technical values.
- **error** turns the field red and replaces **hint** below it.
- Spreads native input props (`type`, `value`, `onChange`, `placeholder`, …).
