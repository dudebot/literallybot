# Testing

Run the suite from the repository root:

```bash
venv/bin/python -m pip install -r requirements-dev.txt
venv/bin/python -m pytest tests/
```

Tests never need live bot credentials, a Discord connection, or the
deployment's `configs/` directory. Use the real `Config` with temporary
storage; mock only external I/O.

`tests/test_architecture.py` enforces cog ownership statically: no core-to-cog
or cog-to-cog imports, Cog classes and Discord feature declarations live in
`cogs/`, and behavioral `@op` declarations sit on owning Cog methods.

## Test quality and development checks

Committed tests need obvious production value: a concrete failure in a
supported code path and a result that identifies what actually broke.
"It could regress" is not enough. A feature or bug fix does not call for
adding permanent tests by default. There is no count target.

Task-specific simulations and model-response/request experiments are
development checks. Implementation mirrors, trivial forwarding checks and
schema/metadata inventories do not belong in the committed suite. Keep
development checks outside the repository and delete them when the
investigation ends, before committing or handing off. Do not turn them into
committed fixtures or retain them because they passed or were useful during
implementation.

Prefer an existing test that already exercises the failure. Keep external
I/O mocked and local behavior real; never load deployment credentials or
config. Remove unused fixtures with deleted tests. Do not merge unrelated
checks, add parameter matrices, or preserve speculative cases to defend a
test count. Run the retained suite after changes.

A new cog or op does not need its own schema, metadata, forwarding and payload
test bundle. Passing tests do not show that a model responds well or that live
Discord behavior works; check those with disposable replays or in a test
server.
