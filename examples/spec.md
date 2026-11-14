# Calculator API specification

- `add(a, b)` returns the arithmetic sum of two numbers.
- `divide(a, b)` returns `a / b` for nonzero `b`.
- `divide(a, 0)` MUST raise `ValueError`.

The agent should generate concise Pytest cases, run them, and classify observed
failures separately from proposed root causes.
