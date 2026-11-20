# Contributing to Meshlytics

Thank you for your interest in Meshlytics. Bug reports, questions, suggestions and code changes are welcome.

## Before you post: no private data

Issues and pull requests are public. Do **not** include API keys, `config.json`, e-mail addresses of others,
patient or participant information, or unpublished/confidential datasets. PubMed IDs, MeSH terms, commands,
error messages and small synthetic examples are fine.

## Reporting a bug or asking a question

Open an [issue](https://github.com/i3-research/meshlytics/issues) and choose a template. Please include:

- the command you ran and the full error message or unexpected output;
- `meshlytics --version`, your Python version and operating system;
- if possible, a small set of public PubMed IDs that shows the problem.

## Suggesting a feature

Open an issue with the feature template and describe the analysis you want to do and why. Meshlytics focuses on
MeSH-based analysis of PubMed literature; please say how the feature relates to that.

## Changing the code

1. Fork the repository and create a branch from `main`.
2. Install in editable mode with the development tools:

   ```
   python -m pip install -e ".[dev,llm]"
   ```

3. Make your change. Keep the scope small; one topic per pull request.
4. Add or update tests in `tests/`. Tests must run offline: replace web services with fakes, as the existing tests
   do (see `tests/conftest.py`, which blocks network access).
5. Check:

   ```
   python -m pytest
   ruff check .
   ```

6. Add a line to the topmost (unreleased) section of `CHANGELOG.md` for any user-visible change.
7. Open a pull request and fill in the template. Continuous integration runs the tests on Windows and Linux
   with Python 3.10-3.12 and the linter; please make sure it passes.

### Conventions

- Python 3.10+; style is checked with ruff (settings in `pyproject.toml`).
- Results must be reproducible: avoid unordered iteration over sets or dicts when it affects output order, and
  break ties explicitly (e.g. by name).
- Do not change output file names or column names without noting it in `CHANGELOG.md`; users' scripts depend on them.
- Figures use the shared style in `src/meshlytics/plot_style.py` (see `docs/FIGURES.md`).
- Respect the NCBI E-utilities usage rules: identify the tool and contact e-mail, and stay within 3 requests per
  second (10 with an API key).

## Updating the MeSH data

See `src/meshlytics/essentials/README.md` for how the MeSH dictionary is built from the NLM descriptor file and how
to update it for a new MeSH year.

## Code of conduct

Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).
