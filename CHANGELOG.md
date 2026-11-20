# Changelog

All notable changes to Meshlytics are listed here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-20

First public release.

### Commands

- `fetch`: PubMed metadata (title, DOI, authors and affiliations, journal, year, abstract, publication
  types, MeSH descriptors with their MeSH IDs) for a list of PMIDs (`.txt`) or for the citing papers of
  cohort papers listed in an Excel workbook (one sheet per cohort, citing papers stored once across cohorts).
  Non-original publication types are skipped and listed; reviews are kept and flagged (`review_paper`).
- `mesh-count`: table of all MeSH descriptors in a dataset with counts and all MeSH tree positions.
- `filter-data`: removes MeSH terms marked in the keyword table and/or whole MeSH tree branches
  (`--exclude-categories`).
- `top-cited`: dataset of the N most cited papers of a PubMed-indexed journal in a year window
  (OpenAlex ranking, PubMed screening with backfill, OpenAlex and PubMed citation counts).
- `institution-processing`: standardised short institution names with an OpenAI model (optional).
- `analyze`: publications per year, entropy-based time windows, MeSH term trends, stacked top-term chart
  and term-by-year heatmap, wordclouds, MeSH category co-occurrence at tree levels 1-3, MeSH term,
  author and institution networks, top journals, authors and institutions.

### Changes compared with the internal versions

- Fixed: papers whose PubMed issue date is given as text (e.g. "2023 Nov-Dec") had no publication year and were
  left out of all time-based analyses. The year now comes from the issue date, its text form, or the electronic
  publication date, in that order (`fetch` and `top-cited`).
- Installable Python package with a `meshlytics` command (`python -m meshlytics` also works);
  requires Python 3.10 or newer. Dependencies are declared in `pyproject.toml` with tested version
  ranges; the start-up check reports missing libraries instead of installing them.
- The OpenAI client and `xlsxwriter` are only needed for `institution-processing` and are installed
  with the optional extra: `pip install "meshlytics[llm]"`.
- Unused dependencies `seaborn` and `colorcet` removed.
- Offline test suite (`pytest`, in `tests/`): MeSH tree mapping, mesh-count, filter-data, category
  co-occurrence at tree levels 1-3, PubMed record parsing and screening, cross-cohort de-duplication,
  top-cited matching and backfill, institution processing, configuration, input-file search and an
  end-to-end `analyze` run. Web services are replaced by fakes; network access is blocked during tests.
- Calls deprecated in pandas 3.1 (`ExcelFile.parse`, `inplace=True`) replaced; results unchanged.
- Continuous integration (GitHub Actions): ruff lint, tests on Ubuntu and Windows with Python 3.10-3.12,
  and a test run with the oldest supported dependency versions.
- Code cleaned to pass `ruff check` (unused imports and variables, style); results unchanged.
- README rewritten for public use (installation, configuration, quick start, commands, outputs, MeSH data,
  web services and data sent); `docs/FIGURES.md` rewritten as a figure reference with the exact network rules.
- Licence: PolyForm Noncommercial License 1.0.0 (`LICENSE`).
- Citation metadata (`CITATION.cff`, `.zenodo.json`), contributing guide, code of conduct, security policy, and
  issue and pull-request templates. A test keeps the version and authors consistent across these files.
- README example figures (`docs/images/`) from the quick-start run.
- The wordcloud of all papers names the years covered (e.g. "2015-2025") instead of "all years".
- `--help` texts corrected where they did not match the code (e.g. `--exclude-current-year` removes the current
  calendar year; `-mesh-ng` example).
- Credentials from environment variables (`NCBI_EMAIL`, `NCBI_API_KEY`, `OPENALEX_API_KEY`,
  `OPENAI_API_KEY`) or a JSON config file (`--config`, `MESHLYTICS_CONFIG`, `./config.json`).
  `analyze`, `filter-data` and `mesh-count` need no configuration.
- NCBI E-utilities requests send the tool name and the user's contact email; the contact email is
  required for `fetch` and `top-cited`.
- MeSH on Demand (suggested MeSH terms for papers not yet indexed) is opt-in (`--mesh-on-demand`),
  experimental, and identifies itself instead of imitating a browser.
- Network figures, community summaries and pair tables are reproducible from run to run.
- Journals, authors, institutions and MeSH terms with equal counts are ranked in name order, so
  count tables and top-N charts do not depend on the installed pandas version.
- `-f <name>` searches the current folder and its subfolders only.
- Figures: one shared style (Arial, colour-blind-safe palettes, 300 dpi PNG by default) with
  `--fig-dpi`, `--fig-format`, `--font-scale`, `--network-labels`, `--cooccurrence-top-categories`;
  see `docs/FIGURES.md`.
- Reference data: MeSH 2026 descriptor dictionary and the NLM journal list are shipped with the package.
