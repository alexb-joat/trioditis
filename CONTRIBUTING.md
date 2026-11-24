# Contributing

Thanks for helping. Trioditis sits between people and every model they use, so the bar for changes is "small, tested, and no new attack surface without a reason".

## Workflow

1. **Fork** the repo and create a branch: `git switch -c fix/short-description`.
2. **Set up** (hash-verified install):
   ```bash
   python3 -m venv .venv && source .venv/bin/activate     # Windows: .\.venv\Scripts\Activate.ps1
   pip install --require-hashes --no-deps -r requirements-dev.txt
   ```
3. **Make the change** and add tests in `tests/`.
4. **Check** before pushing:
   ```bash
   ruff check .                # lint: pyflakes, bugbear, import order and the bandit security rules (pyproject.toml)
   python -m pytest -q
   python scripts/smoke.py
   ```
5. **Open a pull request** and fill in the template. CI, CodeQL and dependency review run automatically; for first-time contributors a maintainer approves the run first.

## Ground rules

- **Dependencies.** A new package needs a justification in the PR. Regenerate both lockfiles with [uv](https://docs.astral.sh/uv/):
  ```bash
  uv pip compile requirements.in --universal --generate-hashes --python-version 3.11 -o requirements.txt
  uv pip compile requirements-dev.in --universal --generate-hashes --python-version 3.11 -o requirements-dev.txt
  ```
- **Network.** No new outbound calls (telemetry, update checks, remote config) without a discussion first.
- **Logging.** Never log prompt text, response text or keys by default.
- **Secrets.** Never commit `.env`, `config.yaml`, keys or real prompts — not even in tests. If one slips into a pushed commit, rotate it: forks keep history even after you delete it.
- **Security issues** go to the Security tab ("Report a vulnerability"), not public issues.

## How contributions are licensed

- Trioditis is under the [GNU AGPL-3.0-or-later](LICENSE), and your contribution is distributed under it.
- Code and documentation also need the [contributor agreement](CLA.md): you keep your copyright and give the maintainer a licence to use, distribute and sublicense what you contribute. Read it, then tick its box in your first pull request. It covers your later ones too.
- A pull request without the tick is not merged. Reporting a bug or suggesting a change in an issue needs no agreement.
- Results you share through the "Share results" issue form are public and released under [CC0](https://creativecommons.org/publicdomain/zero/1.0/): anyone may use the numbers, with no credit needed. The form's box says so before you submit, and `scripts/collect_results.py` adds only issues with the box ticked to `docs/community-results.csv`.

## Style

Small functions, type hints, plain names. Match the code around you.

## Releasing (maintainer)

1. Bump `__version__` in `trioditis/__init__.py` (the only place it is written) and add the `CHANGELOG.md` section.
2. Push, wait for CI, then `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.
3. `.github/workflows/release.yml` checks the tag against the version, builds the sdist and wheel with hash-pinned
   tools, installs the wheel in a clean venv (`init`, `check`), publishes to PyPI through trusted publishing (no
   token exists) and attaches the files to the GitHub release. Run it by hand for a dry run that only builds.
