"""
cli.py

Command-line interface of Meshlytics: argument parsing (parse_arguments) and command dispatch (main).
Commands: fetch, mesh-count, institution-processing, top-cited, filter-data, analyze.
Run as `meshlytics <command> ...` or `python -m meshlytics <command> ...`.

Authors: Ankush Kesri, Stefanie Perrier, P. Ellen Grant, Sarah U. Morton, Yangming Ou.
"""

import argparse
import logging
import os
import sys
from importlib.resources import files

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

# Reference data shipped with the package (see essentials/README.md)
# MeSH version used everywhere (built with scripts/build_mesh_dictionary.py)
MESH_DICTIONARY_FILE = 'MeSH_Dictionary_2026.json'
data_dir = str(files("meshlytics").joinpath("essentials"))
mesh_dictionary_path = os.path.join(data_dir, MESH_DICTIONARY_FILE)
keys_and_classes_path = os.path.join(data_dir, 'keys_and_classes.json')
pubmed_journals_path = os.path.join(data_dir, 'pubmed_journals_11062026.json')

def parse_arguments(argv=None):
    """
    Set up the CLI parser and define subcommands with their arguments.

    Returns:
        argparse.Namespace: Parsed arguments from the command line.
    """
    parser = argparse.ArgumentParser(
        prog='meshlytics',
        description='Meshlytics: MeSH-hierarchy-aware analysis of PubMed literature',
        usage='%(prog)s {fetch,mesh-count,institution-processing,filter-data,analyze,top-cited} [options] --help for details'
    )
    from meshlytics import __version__
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--config', dest='config', default=None,
                        help='JSON file with email and API keys (default: MESHLYTICS_CONFIG, else ./config.json); '
                             'environment variables NCBI_EMAIL, NCBI_API_KEY, OPENALEX_API_KEY, OPENAI_API_KEY take priority')
    subparsers = parser.add_subparsers(dest='command', help='Available commands', required=True)

    # --------------------------- FETCH ---------------------------
    fetch_parser = subparsers.add_parser('fetch', help='Fetch data and update Excel file or fetch using list of PubMed IDs')
    fetch_parser.add_argument('-f', '--file', required=True, help='Path to Excel (.xlsx) or PubMed ID list (.txt) file')
    fetch_parser.add_argument('--mesh-on-demand', dest='mesh_on_demand', action='store_true',
                              help='Experimental: for papers not yet indexed in PubMed (no MeSH terms), request suggested MeSH terms '
                                   'from NLM MeSH on Demand (about 30-60 s per paper; off by default)')

    # ------------------ Generate Mesh Counts file ----------------
    mesh_count_parser = subparsers.add_parser('mesh-count', help='Generates MeSH counts with keys and MeSH tree path at any stage of analysis')
    mesh_count_parser.add_argument('-f', '--file', required=True, help='Excel file path with MeSH data (full path, or just the file name: it is searched in the current folder and its subfolders, incl. TopCited_* folders)')

    # ----------------------- Institutions ------------------------
    institution_parser = subparsers.add_parser('institution-processing', help='Standardizes and shortens institution names with an OpenAI model (default gpt-4o; OPENAI_MODEL to change). Needs OPENAI_API_KEY')
    institution_parser.add_argument('-f', '--file', required=True, help='Excel file with fetched or filtered data (full path, or just the file name: it is searched in the current folder and its subfolders, incl. TopCited_* folders)')

    # ------------------------- TOP CITED -------------------------
    top_cited_examples = r"""
examples:
  Top 100 most cited papers of The Lancet, last 10 complete years (default window):
    meshlytics top-cited -j "lancet"

  Misspelled name: closest PubMed journals are shown for confirmation:
    meshlytics top-cited -j "new england jornal of medicne" -n 50

  Fixed publication window (inclusive):
    meshlytics top-cited -j "CA Cancer J Clin" -n 100 --year-range 2016 2025

  Last 5 complete years, reviews kept (default: reviews excluded and backfilled):
    meshlytics top-cited -j "Nat Neurosci" -n 200 --years-back 5 --include-reviews

  By ISSN, no confirmation prompt (for scripts/batch runs), output under a chosen folder:
    meshlytics top-cited --issn 0140-6736 -n 100 -y -o TopCited_runs

  Then analyze the result:
    meshlytics analyze -f "<output folder>\<...>_pmids_fetched_data.xlsx" -hg -wc -journals

journal prompt:  Select journal [1-N] (Enter = 1), 'r' to retype, 'q' to quit
"""
    top_cited_parser = subparsers.add_parser(
        'top-cited',
        prog='meshlytics top-cited',
        help='Build a dataset of the Top N most cited papers of a PubMed-indexed journal (OpenAlex ranking, PubMed metadata). Needs OPENALEX_API_KEY',
        description='Top N most cited papers of a PubMed-indexed journal: journal confirmation -> OpenAlex ranking (cited_by_count) -> PubMed screening with backfill -> full PubMed metadata for analyze.',
        epilog=top_cited_examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    journal_group = top_cited_parser.add_mutually_exclusive_group(required=True)
    journal_group.add_argument('-j', '--journal', help='Journal name or abbreviation (spelling errors tolerated, closest matches are shown for confirmation)')
    journal_group.add_argument('--issn', help='Journal ISSN (print or online), e.g. 0140-6736')
    top_cited_parser.add_argument('-n', '--top-n', dest='top_n', type=int, default=100, help='Number of most cited papers in the final dataset (default: 100)')
    top_cited_parser.add_argument('--years-back', dest='years_back', type=int, default=10, help='Last N complete years, current year excluded (default: 10)')
    top_cited_parser.add_argument('--year-range', dest='year_range', nargs=2, type=int, metavar=('FROM', 'TO'), help='Manual publication year range, inclusive (e.g. 2016 2025); overrides --years-back')
    top_cited_parser.add_argument('--include-reviews', dest='include_reviews', action='store_true', help='Keep review papers (default: reviews are excluded and replaced by the next most cited papers)')
    top_cited_parser.add_argument('--mesh-on-demand', dest='mesh_on_demand', action='store_true', help='Experimental: request MeSH on Demand terms for selected papers not yet indexed in PubMed (off by default)')
    top_cited_parser.add_argument('-y', '--yes', dest='assume_yes', action='store_true', help='Non-interactive: accept the journal only if there is a single exact match')
    top_cited_parser.add_argument('-o', '--output-dir', dest='output_dir', default=None, help='Parent folder for the output folder (default: current folder)')

    # ------------------------ FILTER DATA ------------------------
    filter_parser = subparsers.add_parser('filter-data', help='Filter data based on MeSH keywords')
    filter_parser.add_argument('-f', '--file', required=True, help='Input Excel file path (full path, or just the file name: it is searched in the current folder and its subfolders, incl. TopCited_* folders)')
    filter_parser.add_argument('-k', '--keywords', default=None, help="Keywords file (txt, or Excel with 'filter_out_term?'). Default: mesh_terms_with_keys_and_counts*.xlsx beside the input file (one with marked terms preferred)")
    filter_parser.add_argument('-o', '--output', default=None, help='Output Excel file. Default: <input name>_filtered.xlsx beside the input file; a name without folder is also saved beside the input file')
    filter_parser.add_argument('--exclude-categories', nargs='*', default=[], help="Optional: MeSH categories to exclude (e.g. A Z V)")
    filter_parser.add_argument('--special-terms', nargs='*', default=[], help="Optional: Specific MeSH terms to exclude (e.g. Male Female Humans)")
    filter_parser.add_argument('--category-match', dest='category_match', choices=['all', 'any'], default='all',
                               help="How --exclude-categories treats terms in several MeSH branches: 'all' (default) removes a term only if "
                                    "all its tree paths are in excluded branches; 'any' removes it if any path is")


    # -------------------------- ANALYZE --------------------------
    analyze_parser = subparsers.add_parser('analyze', help='Analyze MeSH term data (results in a new Analysis_<data name>_<date>_<time> folder beside the data file)',
        epilog=r"""Examples:
  meshlytics analyze -f NatNeurosci_Top300_pmids_fetched_data -hg -wc -journals
  meshlytics analyze -f data.xlsx -hg -mesh-tl -mesh-stacked --top-stacked 7 --fig-format png pdf
  meshlytics analyze -f data.xlsx -mesh-gn -authors-gn --network-labels 15 --fig-dpi 600
  meshlytics analyze -f data.xlsx -co --cooccurrence-top-categories 25 --font-scale 1.2

Figures: 300 dpi PNG by default; see docs/FIGURES.md for the figure options.""",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    analyze_parser.add_argument('-f', '--file', required=True, help='Path to filtered Excel file (full path, or just the file name: it is searched in the current folder and its subfolders, incl. TopCited_* folders)')
    analyze_parser.add_argument('-c', '--cohort', nargs='+', help='Analyze only these cohort sheets (e.g. CohortA CohortB) or a .txt file with one cohort name per line')
    analyze_parser.add_argument('--exclude-current-year', dest='exclude_current_year', action='store_true',help='Exclude papers from the current calendar year (still incomplete)')
    analyze_parser.add_argument('--exclude-review-papers', dest='exclude_review_papers', action='store_true',help='Exclude the review papers from analysis')

    # Flags
    analyze_parser.add_argument('-hg', '--histogram', action='store_true', help='Generate histogram of publication years')
    analyze_parser.add_argument('-co', '--cooccurance', action='store_true', help='Co-occurrence of MeSH categories at tree levels 1-3 (CSV matrices, heatmaps for levels 1 and 2)')
    analyze_parser.add_argument('-wc', '--wordcloud', action='store_true', help='Generate wordcloud')

    # Trend line (Top N MeSH Terms)
    analyze_parser.add_argument('-mesh-tl', '--mesh-trend-line', dest='trend_line', action='store_true', help="Plot trend of top MeSH terms per year")
    analyze_parser.add_argument('--top-trend', dest='top_trend', type=int, default=5, help="Number of top MeSH terms to plot (default: 5)")
    analyze_parser.add_argument('--trend-mode', dest='trend_mode', choices=['total_frequency', 'yearly_presence'], default='total_frequency', help="How the top terms are chosen: total_frequency (default, most mentions overall) or yearly_presence (most often in a year's top N)")

    # Ngram comparison (Custom MeSH terms)
    analyze_parser.add_argument('-mesh-ng', '--mesh-ngram', dest='mesh_ngram', nargs='+', help="Plot the yearly frequency of chosen MeSH terms (e.g. -mesh-ng Brain Sleep)")
    analyze_parser.add_argument('--ngfile', type=str, help="Path to .txt file with MeSH terms (one per line)")
    analyze_parser.add_argument('--ngnorm', action='store_true', help="Normalize ngram frequency")

    # Stacked bar of top MeSH terms
    analyze_parser.add_argument('-mesh-stacked', action='store_true', help="Plot stacked relative frequency of top MeSH terms per year")
    analyze_parser.add_argument('--top-stacked', dest='top_stack', type=int, default=5, help="Number of top terms per year (default: 5)")
    analyze_parser.add_argument('--mesh-stacked-font-size', dest='mesh_stacked_font_size', type=float, default=None, help="Font size of term labels inside the bars (default: automatic, labels only where they fit)")
    analyze_parser.add_argument('--mesh-stacked-rotation', dest='mesh_stacked_rotation', type=float, default=0, help="Rotation (degrees) of term labels inside the bars (default: 0 = automatic horizontal labels)")

    # Top Journals
    analyze_parser.add_argument('-journals', dest='journals', action='store_true', help="Plot Top journals where the literature was published")
    analyze_parser.add_argument('--top-journals', dest='top_journals', type=int, default=20, help="Number of top journals where literature was published")

    # Top Authors
    analyze_parser.add_argument('-authors', dest='authors', action='store_true', help="Plot Top authors with high publication count")
    analyze_parser.add_argument('--top-authors', dest='top_authors', type=int, default=20, help="Number of top authors with high publication count")
    analyze_parser.add_argument('--first-author-only', dest='first_author_only', action='store_true',help='Plot Top n authors (Only First author of the paper is considered)')
    analyze_parser.add_argument('--include-institution-name', dest='include_institution_name', action='store_true',help='Includes the institution name of author (Most frequent if multiple institutions)')

    # Top Institutions
    analyze_parser.add_argument('-institutions', dest='institutions', action='store_true', help="Plot Top institutions (first author) with high publication count")
    analyze_parser.add_argument('--top-institutions', dest='top_institutions', type=int, default=20, help="Number of top institutions with high publication count")

    # Authors collaboration graph network
    analyze_parser.add_argument('-authors-gn', '--authors-graphnetwork', dest='authors_gn', action='store_true', help='Generate authors collaboration graph network')
    analyze_parser.add_argument('--top-authors-gn', dest='top_authors_gn', type=int, default=150, help='Top N Authors for graph network (default: 150)')
    analyze_parser.add_argument('--authors-gn-percentile', '--authors-gn-edge-threshold-percentile', dest='authors_gn_percentile', type=float, default=95.0, help='Edge percentile threshold (default: 95)')
    analyze_parser.add_argument('--authors-gn-thickness', '--authors-gn-edge-thickness-multiplier', dest='authors_gn_thickness', type=float, default=5.0, help='Edge thickness multiplier (default: 5)')
    analyze_parser.add_argument('--authors-gn-degree', '--authors-gn-min-degree', dest='authors_gn_degree', type=int, default=2, help='Min degree to keep node (default: 2)')

    # Institutions collaboration graph network
    analyze_parser.add_argument('-institutions-gn', '--institutions-graphnetwork', dest='institutions_gn', action='store_true', help='Generate institutions collaboration graph network')
    analyze_parser.add_argument('--top-institutions-gn', dest='top_institutions_gn', type=int, default=150, help='Top N Institutions for graph network (default: 150)')
    analyze_parser.add_argument('--institutions-gn-percentile', '--institutions-gn-edge-threshold-percentile', dest='institutions_gn_percentile', type=float, default=95.0, help='Edge percentile threshold (default: 95)')
    analyze_parser.add_argument('--institutions-gn-thickness', '--institutions-gn-edge-thickness-multiplier', dest='institutions_gn_thickness', type=float, default=5.0, help='Edge thickness multiplier (default: 5)')
    analyze_parser.add_argument('--institutions-gn-degree', '--institutions-gn-min-degree', dest='institutions_gn_degree', type=int, default=2, help='Min degree to keep node (default: 2)')

    # Graph network options
    analyze_parser.add_argument('-mesh-gn', '--mesh-graphnetwork', dest='mesh_gn', action='store_true', help='MeSH term co-occurrence network with communities (plus pair tables and Gephi files)')
    analyze_parser.add_argument('--generate-full-matrix', dest='generate_full_matrix', action='store_true', help='Also save full MeSH co-occurrence matrix')
    analyze_parser.add_argument('--top-mesh-gn', dest='top_mesh_gn', type=int, default=200, help='Top N MeSH terms for graph network (default: 200)')
    analyze_parser.add_argument('--mesh-gn-percentile', '--mesh-gn-edge-threshold-percentile', dest='mesh_gn_percentile', type=float, default=95.0, help='Edge percentile threshold (default: 95)')
    analyze_parser.add_argument('--mesh-gn-thickness', '--mesh-gn-edge-thickness-multiplier', dest='mesh_gn_thickness', type=float, default=5.0, help='Edge thickness multiplier (default: 5)')
    analyze_parser.add_argument('--mesh-gn-degree', '--mesh-gn-min-degree', dest='mesh_gn_degree', type=int, default=2, help='Keep MeSH terms with more than N links (default: 2)')

    # Figure output (all analyze figures)
    analyze_parser.add_argument('--network-labels', dest='network_labels', type=int, default=None, help='Label the N best-connected nodes in the MeSH/author/institution networks (default: all MeSH terms, 30 authors, 20 institutions; 0 = no labels; -1 = all)')
    analyze_parser.add_argument('--cooccurrence-top-categories', dest='cooccurrence_top_categories', type=int, default=40, help='Level 2 co-occurrence heatmap: show the N categories with the most co-occurrence (default: 40; the CSV always has all)')
    analyze_parser.add_argument('--fig-dpi', dest='fig_dpi', type=int, default=300, help='Resolution of PNG figures (default: 300; use 600 for print)')
    analyze_parser.add_argument('--fig-format', dest='fig_format', nargs='+', default=['png'], choices=['png', 'pdf', 'svg'], help='Figure file format(s), e.g. --fig-format png pdf (default: png). PDF/SVG keep text editable')
    analyze_parser.add_argument('--font-scale', dest='font_scale', type=float, default=1.0, help='Scale all figure fonts, e.g. 1.2 for slides (default: 1.0; base sizes 13/11/10 pt)')

    # Histogram & Wordcloud time intervals
    analyze_parser.add_argument('--year-range', nargs=2, type=int, help='Manual year range (e.g. 2015 2022)')
    analyze_parser.add_argument('--use-entropy-window', action='store_true', help='Use entropy-based window instead of fixed intervals')
    analyze_parser.add_argument('--longitudinal-wordcloud', action='store_true', help='Generate multiple wordclouds over time intervals')
    analyze_parser.add_argument('--fixed-window-size', type=int, help='Fixed window size (e.g. 5 for 5-year intervals)')
    analyze_parser.add_argument('--entropy-min-window', type=int, default=3, help='Min window size for entropy-based search')
    analyze_parser.add_argument('--entropy-max-window', type=int, default=6, help='Max window size for entropy-based search')

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_arguments(argv)

    # Credentials: environment variables and/or a config file (see config.py). Only the commands that
    # call web services need them, so analyze / filter-data / mesh-count run without any configuration.
    from meshlytics import config
    try:
        config.load(args.config)
    except (FileNotFoundError, ValueError) as e:
        logging.error(f"Could not read the config file: {e}")
        sys.exit(1)
    email = config.email
    api_key = config.api_key
    open_ai_api_key = config.open_ai_api_key
    openalex_api_key = config.openalex_api_key
    if args.command in ('fetch', 'top-cited'):
        if not email:
            logging.error("NCBI requires a contact email for E-utilities. Set NCBI_EMAIL or 'email' in the config file "
                          "(see config.example.json).")
            sys.exit(1)
        if api_key is None:
            logging.warning("No NCBI API key: PubMed requests are limited to 3 per second "
                            "(set NCBI_API_KEY or 'api_key' for 10 per second).")

    # Dependency check before the analysis modules are imported
    from meshlytics.dependencies import ensure_required_libraries
    ensure_required_libraries(include_llm=(args.command == 'institution-processing'))

    from meshlytics import fetch, top_cited
    from meshlytics.filter_data import run_filter_data
    from meshlytics.analyze import run_analysis
    from meshlytics.utils import resolve_input_file

    # Commands that read a data file: if the -f path does not exist, the file is searched by name
    # (with or without .xlsx) in the current folder and its subfolders, including output
    # folders such as TopCited_*; the file used is printed with its location.
    if args.command in ('mesh-count', 'institution-processing', 'filter-data', 'analyze'):
        try:
            args.file = resolve_input_file(args.file, extensions=(".xlsx",))
        except FileNotFoundError as e:
            logging.error(str(e))
            sys.exit(1)

    if args.command == 'fetch':
        if args.file.endswith(".txt"):
            fetch.fetch_data_from_txt(args.file, email, api_key, mesh_dictionary_path, use_mesh_on_demand=args.mesh_on_demand)
        elif args.file.endswith(".xlsx"):
            fetch.fetch_data(args.file, email, api_key, mesh_dictionary_path, use_mesh_on_demand=args.mesh_on_demand)
        else:
            logging.error("Unsupported file type. Only .xlsx and .txt files are accepted.")
            sys.exit(1)

    elif args.command == 'mesh-count':
        fetch.generate_mesh_terms_with_keys_and_counts(args.file, mesh_dictionary_path)

    elif args.command == 'institution-processing':
        if not open_ai_api_key:
            logging.error("Institution processing requires an OpenAI API key: set OPENAI_API_KEY or 'open_ai_api_key' in the config file.")
            sys.exit(1)
        from meshlytics import institution_processing
        institution_processing.main(args.file, open_ai_api_key, model=config.openai_model)

    elif args.command == 'top-cited':
        if not openalex_api_key:
            logging.error("top-cited requires an OpenAlex API key: set OPENALEX_API_KEY or 'openalex_api_key' in the config file.")
            sys.exit(1)
        try:
            top_cited.run_top_cited(
                journal_query=args.journal if args.journal else args.issn,
                email=email,
                api_key=api_key,
                openalex_api_key=openalex_api_key,
                mesh_dictionary_path=mesh_dictionary_path,
                journals_path=pubmed_journals_path,
                top_n=args.top_n,
                years_back=args.years_back,
                year_range=args.year_range,
                include_reviews=args.include_reviews,
                assume_yes=args.assume_yes,
                output_dir=args.output_dir,
                use_mesh_on_demand=args.mesh_on_demand,
            )
        except ValueError as e:
            # Input problems (ambiguous journal with --yes, invalid year range, ...): message only, no traceback
            logging.error(str(e))
            sys.exit(1)

    elif args.command == 'filter-data':
        try:
            run_filter_data(args)
        except FileNotFoundError as e:
            # e.g. no keyword file beside the data file: message only, no traceback
            logging.error(str(e))
            sys.exit(1)

    elif args.command == 'analyze':
        from meshlytics.plot_style import configure_figures
        configure_figures(dpi=args.fig_dpi, formats=args.fig_format, font_scale=args.font_scale)
        # Prepare ngram terms
        ng_terms = args.mesh_ngram if args.mesh_ngram else []
        if args.ngfile:
            try:
                with open(args.ngfile, 'r') as f:
                    file_terms = [line.strip() for line in f if line.strip()]
                    ng_terms.extend(file_terms)
            except Exception as e:
                print(f"[ERROR] Cannot read --ngfile: {e}")
                ng_terms = []

        run_analysis(
                file=args.file,
                keys_and_classes_path=keys_and_classes_path,
                mesh_dictionary_path=mesh_dictionary_path,
                cohort=args.cohort,
                histogram=args.histogram,
                cooccurance=args.cooccurance,
                wordcloud=args.wordcloud,
                exclude_current_year=args.exclude_current_year,
                exclude_review_papers=args.exclude_review_papers,

                # Trend Line
                trend_line=args.trend_line,
                top_trend=args.top_trend,
                trend_mode=args.trend_mode,

                # Ngram
                ng_terms=ng_terms,
                normalize_ng=args.ngnorm,

                # Stacked bar
                mesh_stacked=args.mesh_stacked,
                top_stack=args.top_stack,
                mesh_stacked_font_size=args.mesh_stacked_font_size,
                mesh_stacked_rotation=args.mesh_stacked_rotation,

                # Graph Network
                mesh_gn=args.mesh_gn,
                generate_full_matrix=args.generate_full_matrix,
                top_mesh_gn=args.top_mesh_gn,
                mesh_gn_percentile=args.mesh_gn_percentile,
                mesh_gn_thickness=args.mesh_gn_thickness,
                mesh_gn_degree=args.mesh_gn_degree,

                # Histogram/wordcloud time slicing
                year_range=args.year_range,
                use_entropy_window=args.use_entropy_window,
                longitudinal_wordcloud=args.longitudinal_wordcloud,
                fixed_window_size=args.fixed_window_size,
                entropy_min_window=args.entropy_min_window,
                entropy_max_window=args.entropy_max_window,

                # Top Journals
                journals=args.journals,
                top_journals=args.top_journals,

                # Top Authors
                authors=args.authors,
                top_authors=args.top_authors,
                first_author_only=args.first_author_only,
                include_institution_name=args.include_institution_name,

                # Top Institutions
                institutions=args.institutions,
                top_institutions=args.top_institutions,

                # Authors Collaboration Graph Network
                authors_gn=args.authors_gn,
                top_authors_gn=args.top_authors_gn,
                authors_gn_percentile=args.authors_gn_percentile,
                authors_gn_thickness=args.authors_gn_thickness,
                authors_gn_degree=args.authors_gn_degree,

                # Institutions Collaboration Graph Network
                institutions_gn=args.institutions_gn,
                top_institutions_gn=args.top_institutions_gn,
                institutions_gn_percentile=args.institutions_gn_percentile,
                institutions_gn_thickness=args.institutions_gn_thickness,
                institutions_gn_degree=args.institutions_gn_degree,

                # Figure options
                network_labels=args.network_labels,
                cooccurrence_top_categories=args.cooccurrence_top_categories,
            )


    else:
        logging.warning("No valid command provided. Use -h for help.")


if __name__ == '__main__':
    main()
