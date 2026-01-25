# Red Sea ReefBeat - Generated Tests

These tests are intended for `pytest-homeassistant-custom-component`.

## Install test deps

Typical:

- pytest
- pytest-asyncio
- pytest-homeassistant-custom-component

This repo supports two Home Assistant targets via constraints (while `requirements.test.txt` provides minimum versions).

Note: some dependencies are **runtime requirements of the integration** (declared in `custom_components/redsea/manifest.json`).
Home Assistant installs these automatically; for local test runs we install them explicitly.

- **Default (minimums)**: install with lower bounds (pip may choose newer):

  - `pip install -r requirements.test.txt`
  - `python -c "import json; print('\\n'.join(json.load(open('custom_components/redsea/manifest.json'))['requirements']))" | pip install -r /dev/stdin`
- **2025.x compatibility** ("anything 2025 should work"):

  - `python -c "import json; print('\\n'.join(json.load(open('custom_components/redsea/manifest.json'))['requirements']))" | pip install -r /dev/stdin -c constraints-ha2025.txt`
  - `pip install -r requirements.test.txt -c constraints-ha2025.txt`
- **2026.1.x / production**:

  - `python -c "import json; print('\\n'.join(json.load(open('custom_components/redsea/manifest.json'))['requirements']))" | pip install -r /dev/stdin -c constraints-ha2026.txt`
  - `pip install -r requirements.test.txt -c constraints-ha2026.txt`

## Run

pytest -q

## Pre-commit

This repo uses `pre-commit` to run quality checks at commit time.

Hooks configured in [.pre-commit-config.yaml](.pre-commit-config.yaml):

- `ruff-check` (with `--fix`)
- `ruff-format`
- `pyright` (strict)
- `pytest -q`

Setup:

- Install dev/test deps (includes `pre-commit`): `pip install -r requirements.test.txt`
- Enable git hooks: `pre-commit install`
- Run manually: `pre-commit run --all-files` (or `pre-commit run --all-files -v` for more progress output)

Notes:

- The `pytest` hook runs in a pre-commit managed Python environment (local hook with `language: python` + `additional_dependencies`), so it does not depend on your current shell venv.
- After changing hook configuration/dependencies, you may need: `pre-commit clean` (then rerun a commit or `pre-commit run --all-files`).
- Hook versions (e.g. ruff `rev`) are kept up to date by the scheduled workflow in [.github/workflows/pre-commit-autoupdate.yml](.github/workflows/pre-commit-autoupdate.yml).

## Notes

- Network is disabled by monkeypatching `ReefBeatAPI.fetch_data()` to load captured fixture payloads.
- Fixtures included under `tests/fixtures/`.
