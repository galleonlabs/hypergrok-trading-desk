# Contributing

Keep changes original, source-led and testable. Any funds-moving change must
trace the public command to its only signer/send, prove retries cannot submit
twice, and add zero-send assertions for every new failure gate.

Run `ruff check .`, `mypy src` and `pytest` before opening a pull request.
Never commit credentials or captured account payloads.
