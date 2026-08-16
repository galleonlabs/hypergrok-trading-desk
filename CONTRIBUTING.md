# Contributing

Keep changes original, source-led and testable. Do not copy third-party skill prose, prompts, fixtures or code without compatible provenance.

Any funds-moving change must trace the public command to its only signer/send, prove retries cannot submit twice, preserve builder attribution, and add zero-send assertions for every new failure gate.

```bash
python -m pip install -e '.[dev]'
ruff check .
mypy src
pytest -q --cov=hypergrok --cov-fail-under=75
python -m build
```

Never commit credentials, captured account payloads or generated order plans. Use fake SDK modules and API fixtures in tests. Live smoke checks must remain read-only.
