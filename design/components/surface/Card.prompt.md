Surface container — groups related content with optional header/body/footer.

```jsx
<Card>
  <Card.Header title="Provider" actions={<IconButton label="Edit"><EditIcon/></IconButton>} />
  <Card.Body>Anthropic · Claude 3.5 Sonnet</Card.Body>
  <Card.Footer><Button size="sm" variant="secondary">Test connection</Button></Card.Footer>
</Card>

<Card interactive onClick={open}> … </Card>   {/* clickable list card */}
```

- **raised** for floating panels; **interactive** for clickable cards (hover lift).
- Compose with `Card.Header` / `Card.Body` / `Card.Footer`, or just put content directly inside.
