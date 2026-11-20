# Figures produced by `meshlytics analyze`

All figures are drawn with one style module (`src/meshlytics/plot_style.py`). Figure options change only how
results are drawn, not how they are computed. Count tables, co-occurrence matrices and network tables are saved
next to the figures.

## Shared style

| Item | Setting |
|---|---|
| Font | Arial (DejaVu Sans if Arial is not installed); title 13 pt bold, labels 11 pt, ticks and legends 10 pt; scaled with `--font-scale` |
| Layout | Left-aligned bold title and a grey subtitle with the number of papers, the period and any thresholds; no top/right axis lines; light value grid |
| One data series | Blue `#2F5D8C` |
| MeSH terms and categories | Okabe-Ito colours, then Paul Tol muted colours (colour-blind-safe; no yellow, grey or black for terms) |
| Same term, same colour | A term keeps its colour in every figure of a run (the 9 most frequent terms of the dataset get the first colours); further terms are grey and named directly where there is room |
| Continuous values | One light-to-dark blue scale (heatmaps, entropy window sizes); wordclouds use multi-colour (viridis) words |
| Legends | Compact columns below the plot (stacked chart, networks) |
| Numbers | Counts below 10,000 are written in full (1,050), larger counts abbreviated (12.5k) |
| Resolution and formats | 300 dpi PNG by default; `--fig-dpi 600` for print; `--fig-format png pdf svg` for vector files with editable text |

## Figures

| Option | File | What it shows |
|---|---|---|
| `-hg` | `full_histogram.png` | Papers per year, with a count on every bar; the current (incomplete) year in a lighter bar. Spans over 30 years get a tick every 5 years |
| `-hg --use-entropy-window` | `histogram_<from>_<to>.png`, `entropy_trends_by_year_window.png` | The window of 3-6 years (`--entropy-min-window`, `--entropy-max-window`) in which publications are most evenly spread (highest Shannon entropy of the yearly counts), and the entropy of every sliding window |
| `-wc` | `Wordcloud__freq_ge_5.png` | MeSH terms sized by the number of papers tagged with them (terms in at least 5 papers); the title gives the first and last publication year of the papers |
| `-wc --longitudinal-wordcloud` | `Wordcloud_<from>_<to>_*.png` | One wordcloud per time window (entropy-based windows with `--use-entropy-window`, or `--fixed-window-size N`) |
| `-mesh-tl` | `top_<N>_MeSH_terms_trend_<mode>.png` | Yearly counts of the top N terms (`--top-trend`, `--trend-mode`); each line is labelled at its end. A term's count is shown for every year, not only years where it is in the top N |
| `-mesh-ng TERM ...` | `custom_MeSH_terms_trend_<terms>.png` | Yearly counts of chosen terms; with `--ngnorm`, the share of each year's papers (%) |
| `-mesh-stacked` | `stacked_top<N>_terms_relative_sorted_per_year.png`, `stacked_top<N>_terms_relative_per_year.xlsx` | Share of each year's top-N mentions (`--top-stacked`), stacked smallest to largest; term names printed inside a segment where they fit; about 10 terms get distinct colours, the rest are grey |
| `-mesh-stacked` | `top<N>_terms_per_year_heatmap.png` | Companion heatmap: one row per term that enters any year's top N, one column per year, shade and number = share of that year's top-N mentions; readable with 40+ terms. A swatch beside each name shows the term's colour in the stacked chart |
| `-co` | `level1_cooccurrence_heatmap.png`, `level2_cooccurrence_heatmap.png`, `level1/2/3_co_occurrence_matrix.csv` | How often MeSH categories appear together, at tree levels 1-3 (see below). Lower triangle only (the matrix is symmetric); level 1 prints every value; level 2 shows the top 40 categories (`--cooccurrence-top-categories`) on a square-root colour scale. Level 3 is saved as CSV only |
| `-mesh-gn` | `MeSHGraph_*.png` and tables | Network of MeSH terms; a link joins two terms that appear in the same papers, weighted by the number of such papers |
| `-authors-gn` | `AuthorGraph_*.png` and tables | Co-authorship network; a link joins two authors of the same paper, weighted by joint papers |
| `-institutions-gn` | `InstitutionGraph_*.png` and tables | Collaboration network of institutions (needs `institution-processing` first, see below) |
| `-journals` | `Top_<N>_Journals_Bar.png`, `Journal_Counts_All.xlsx` | Journals with the most papers (`--top-journals`, default 20) |
| `-authors` | `Top_<N>_Authors_*.png`, `Author_Counts_*.xlsx` | Most productive authors (`--first-author-only`, `--include-institution-name`) |
| `-institutions` | `Top_<N>_FirstAuthor_Institutions_Bar.png`, `Institution_Counts_All.xlsx` | Institutions of first authors (needs `institution-processing` first) |

Bar charts share one layout: largest at the top, a count at each bar end, long names wrapped. Entries with equal
counts are listed in name order.

### Co-occurrence of MeSH categories (`-co`)

Each descriptor of a paper is placed at **every** position it has in the MeSH tree. For each pair of descriptors in
a paper, every distinct pair of categories they link adds 1, at tree level 1 (e.g. C Diseases), level 2 (e.g. C04
Neoplasms) and level 3 (e.g. C04.588 Neoplasms by Site). Pairs within the same level-1 category are not counted; in
the level 2 heatmap such cells are left blank rather than shown as 0. Descriptors whose own tree number is a level-2
code (e.g. Neoplasms, C04) have no level-3 position.

### Networks (`-mesh-gn`, `-authors-gn`, `-institutions-gn`)

Which nodes and links are drawn (the subtitle of each figure states the rule that was applied):

| Network | Nodes considered | Links kept | Nodes kept |
|---|---|---|---|
| MeSH terms | the `--top-mesh-gn` terms (default 200) with the most co-occurrences | weight at or above the `--mesh-gn-percentile` (default 95) of all link weights | more than `--mesh-gn-degree` links (default 2) |
| Authors | the `--top-authors-gn` best-connected authors (default 150) **and their direct co-authors** | 5,000 or more author pairs: weight at or above the `--authors-gn-percentile` (default 95); fewer pairs: at least 3 joint papers | at least `--authors-gn-degree` links (default 2) |
| Institutions | the `--top-institutions-gn` most frequent institutions (default 150), best-connected first | 300 or more pairs: weight at or above the `--institutions-gn-percentile` (default 95); fewer pairs: at least 3 joint papers | at least `--institutions-gn-degree` links (default 2) |

Institutions come from the `author_institution_llm` column made by `institution-processing` (standardised short
names); without it `-institutions` and `-institutions-gn` are skipped with a message.

- Communities: greedy modularity (NetworkX). Colours by community size (8 colours, then grey "N smaller communities").
  The legend lists the best-connected members of each community.
- Node size = number of links; link width scales with weight (`--*-gn-thickness`).
- Labels: every MeSH term, the 30 best-connected authors and 20 best-connected institutions by default
  (`--network-labels N`; 0 = none, -1 = all). Labels that would still collide are hidden (the less-connected one),
  and the subtitle says so.
- Tables: `*_Communities_Summary.xlsx`, `*_Edges_With_Communities.xlsx`, the pair table, and `*_gephi_nodes.csv` /
  `*_gephi_edges.csv` for Gephi.
- Reproducible: the same data and options give the same layout, communities, labels and files on every run.

## Options

| Option | Default | Purpose |
|---|---|---|
| `--fig-dpi N` | 300 | PNG resolution (600 for print) |
| `--fig-format png pdf svg` | png | One or more formats; PDF/SVG keep text editable |
| `--font-scale X` | 1.0 | Scale all fonts, e.g. 1.2 for slides |
| `--network-labels N` | all / 30 / 20 | Labels in the MeSH / author / institution networks; 0 = none, -1 = all |
| `--cooccurrence-top-categories N` | 40 | Categories shown in the level 2 heatmap (the CSV has all) |
| `--mesh-stacked-font-size X` | automatic | Size of the term names inside the stacked bars |
| `--mesh-stacked-rotation X` | 0 | Rotation of those names |

Examples:

```
meshlytics analyze -f data.xlsx -hg -mesh-tl -mesh-stacked --top-stacked 7 --fig-format png pdf
meshlytics analyze -f data.xlsx -mesh-gn -authors-gn --network-labels 15 --fig-dpi 600
meshlytics analyze -f data.xlsx -co --cooccurrence-top-categories 25 --font-scale 1.2
```

## Checking figure text

Setting the environment variable `MESHLYTICS_FIG_CHECK=1` before `analyze` reports any figure text that overlaps
other text or falls outside the figure.

## Notes

- The last year of a dataset is often incomplete, so time figures drop there; `--exclude-current-year` removes
  papers from the current calendar year.
- On Windows, very long output paths (over 260 characters) are saved using the long-path form; `analyze` warns when
  the output folder path itself is long.
