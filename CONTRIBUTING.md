# contributing

issues and prs are welcome.

```sh
uv venv --python 3.14 && uv pip install -e '.[dev]'
.venv/bin/python -m pytest -q
helm lint charts/clont      # if you touched the chart
```

- keep clont read-only. a new check gets only describe / list / get calls, and
  its iam actions go into [docs/iam.md](docs/iam.md) in the same pr
- no billed api calls on the default path (cost explorer is opt-in for a reason)
- new behaviour comes with a test, no network in tests
- commit messages: one short line, `feat:` / `fixes:` / `chore:` / `refactor:`
