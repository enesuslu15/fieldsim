# Contributing

Thanks for helping! Bug reports, protocol knowledge ("my PLC actually answers
this with...") and pull requests are all welcome.

## Reporting a bug

Open an [issue](https://github.com/enesuslu15/fieldsim/issues/new/choose) with:

- the fieldsim, Python and OS versions (`fieldsim --version`),
- the smallest device file and test that show the problem,
- what you expected, and what a real device does if you know it.

## Development setup

```bash
git clone https://github.com/enesuslu15/fieldsim
cd fieldsim
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[dev,docs]"
pre-commit install
```

## Checks

CI runs exactly these; please run them before opening a pull request:

```bash
ruff check . && ruff format --check .
mypy
coverage run -m pytest && coverage report     # must stay at 90 % or more
pytest examples
mkdocs build --strict                         # if you touched the docs
```

`coverage run` is used instead of `pytest --cov` because fieldsim's pytest
plugin is imported (through its entry point) before pytest-cov starts.

## Guidelines

- **Test against an independent client.** Protocol behaviour is verified with
  pymodbus, asyncua's client or snap7's client, never only with our own code.
- **Keep the YAML strict.** Unknown keys are errors and every mistake should be
  reported with its location. Add a test for each new validation message.
- **Timing tests need slack.** Windows timers tick every ~16 ms; prefer the
  virtual clock, and leave margin where real time is unavoidable.
- **Commit messages** follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `test:`, ...), which drive the changelog and the
  version number.
- New user-visible behaviour needs a line in `CHANGELOG.md` and, usually, a
  docs page update.

## Adding a protocol

See [Adding a protocol](https://enesuslu15.github.io/fieldsim/adding-a-protocol/)
in the documentation. Open an issue first so we can agree on the YAML shape.

## Releasing (maintainers)

1. Update `__version__` in `src/fieldsim/__init__.py` and date the
   `CHANGELOG.md` section.
2. Commit, then tag: `git tag -a vX.Y.Z -m "fieldsim X.Y.Z"` and push the tag.
3. The release workflow publishes to PyPI (Trusted Publishing), creates the
   GitHub release and the Docker workflow pushes the image tags.

By contributing you agree that your contributions are licensed under the MIT
license and that you will follow the
[code of conduct](https://github.com/enesuslu15/fieldsim/blob/main/CODE_OF_CONDUCT.md).
