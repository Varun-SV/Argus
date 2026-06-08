Styled native select — use for short, known option sets (provider, adapter, environment).

```jsx
<Select label="Provider" options={[
  {value:"anthropic", label:"Anthropic — Claude 3.5 Sonnet"},
  {value:"openai", label:"OpenAI — GPT-4o"},
  {value:"ollama", label:"Ollama — LLaVA (local)"},
]} defaultValue="anthropic" />
```

- Pass **options** as `{value,label}[]`, or omit it and pass `<option>` children.
- Omit **label**/**hint** to render the bare control (e.g. inline in a toolbar).
