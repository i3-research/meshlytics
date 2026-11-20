"""
analyze.py

This module analyzes MeSH term data from filtered Excel files by generating:
- Histogram of Publication count w.r.t. time in years
- Wordclouds of frequently occurring MeSH terms
- Longitudinal wordclouds to study MeSH topic relevance or emergence for chosen time intervals
- Co-occurrence matrices of MeSH UIDs based on hierarchy
- MeSH Graph networks for visualizing term relationships
- Top Authors with high publication count bar plot
- Top first author Institutions with high publication count
- Top Journals where the literature got published
- Authors collaboration graph network
- Institutions collboration graph network
- MeSH trendline plot for top MeSH term trend throughout publication time from data
- MeSH N-gram type plot to visualize trendlines for manually selected MeSH terms
- Stacked MeSH plot for Top MeSH terms for each publication year

Outputs are saved in a new folder per run beside the data file:
'Analysis_<data file name>_<MonDD_YYYY>_<HHMMSS>'.
"""


import os
import pandas as pd
from collections import Counter
from datetime import datetime
import matplotlib
matplotlib.use('Agg')

from meshlytics import plot_style  # shared figure theme (fonts, colours, saving); replaces the old global rcParams

from meshlytics.utils import (
    create_output_dir,
    load_excel_data,
    append_analysis_summary,
    plot_histogram,
    generate_wordcloud,
    compute_max_entropy_window,
    get_all_entropy_intervals,
    plot_entropy_trends,
    generate_cooccurrence_matrix,
    generate_topn_mesh_cooccurrence_matrix,
    generate_mesh_graph_network_plot,
    plot_top_terms_per_year,
    plot_custom_terms_trend,
    plot_stacked_terms_relative_per_year,
    plot_top_journals_bar,
    plot_top_authors_bar,
    plot_top_first_author_institutions_bar,
    generate_author_collab_graph_from_pipeline_df,
    generate_institution_collab_graph_from_pipeline_df)


# ------------------------------- Main Run -------------------------------

def run_analysis(
    file,
    cohort=None,
    histogram=False,
    cooccurance=False,
    wordcloud=False,
    mesh_gn=False,
    keys_and_classes_path=None,
    mesh_dictionary_path=None,
    year_range=None,
    use_entropy_window=False,
    longitudinal_wordcloud=False,
    fixed_window_size=None,
    entropy_min_window=3,
    entropy_max_window=6,
    generate_full_matrix=False,
    top_mesh_gn=200,
    mesh_gn_percentile=95.0,
    mesh_gn_thickness=5.0,
    mesh_gn_degree=2,
    trend_line=False,
    top_trend=5,
    trend_mode='total_frequency',
    ng_terms=None,
    normalize_ng=False,
    mesh_stacked=False,
    top_stack=5,
    mesh_stacked_font_size=None,
    mesh_stacked_rotation=0,
    journals=False,
    top_journals=20,
    authors=False,
    top_authors=20,
    first_author_only=False,
    include_institution_name=False,
    institutions=False,
    top_institutions=20,
    authors_gn=False,
    top_authors_gn=150,
    authors_gn_percentile=95.0,
    authors_gn_thickness=5.0,
    authors_gn_degree=2,
    institutions_gn=False,
    top_institutions_gn=150,
    institutions_gn_percentile=95.0,
    institutions_gn_thickness=5.0,
    institutions_gn_degree=2,
    exclude_current_year=False,
    network_labels=None,
    cooccurrence_top_categories=40,
    exclude_review_papers=True
):
    output_dir = create_output_dir(data_file=file)
    print(f"[INFO] Analysis results folder: {output_dir}")

    # --- Cohort handling ---
    cohort_list = None
    if cohort:
        if isinstance(cohort, list) and len(cohort) == 1 and os.path.isfile(cohort[0]):
            with open(cohort[0], "r") as f:
                cohort_list = [line.strip() for line in f if line.strip()]
        else:
            cohort_list = cohort

    df = load_excel_data(file, cohort_list=cohort_list)

    # ---------------------------
    # Drop / filter accounting
    # ---------------------------
    drop_stats = {
        "loaded_rows": len(df),
        "dropped_unparseable_year": 0,
        "dropped_outside_year_range": 0,
        "dropped_current_year": 0,
        "dropped_review_papers": 0,
        "final_analyzed_rows": 0,
    }

    # --- Year parsing ---
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")

    before = len(df)
    df = df.dropna(subset=["year"]).copy()
    drop_stats["dropped_unparseable_year"] = before - len(df)

    # --- Apply year range globally ---
    effective_requested_range = None
    if year_range is not None:
        if not isinstance(year_range, (list, tuple)) or len(year_range) != 2:
            raise ValueError("year_range must be a tuple/list like (from_year, to_year).")

        from_year, to_year = map(int, year_range)

        if from_year > to_year:
            raise ValueError(f"Invalid year_range: start year {from_year} > end year {to_year}")

        before = len(df)
        df = df[(df["year"] >= from_year) & (df["year"] <= to_year)].copy()
        drop_stats["dropped_outside_year_range"] = before - len(df)
        effective_requested_range = (from_year, to_year)

        print(f"[INFO] Applied global year range filter: {from_year}–{to_year}")

    # --- Exclude actual current calendar year ---
    if exclude_current_year and len(df) > 0:
        current_year = datetime.now().year
        before = len(df)
        df = df[df["year"] != current_year].copy()
        drop_stats["dropped_current_year"] = before - len(df)

        if drop_stats["dropped_current_year"] > 0:
            if effective_requested_range and effective_requested_range[1] >= current_year:
                print(
                    f"[INFO] Requested year range included current year {current_year}, "
                    f"but it was excluded because exclude_current_year=True. "
                    f"Effective analyzed range ends at {current_year - 1}."
                )
            else:
                print(f"[INFO] Excluding current year {current_year} from analysis.")

    # --- Exclude review papers ---
    if exclude_review_papers and "review_paper" in df.columns:
        before = len(df)
        df = df[df["review_paper"] == "No"].copy()
        drop_stats["dropped_review_papers"] = before - len(df)

    drop_stats["final_analyzed_rows"] = len(df)

    # --- Year counts ---
    year_counts = df["year"].value_counts().sort_index()

    print(
        "[SUMMARY] Rows loaded: {loaded_rows} | "
        "Dropped (missing year): {dropped_unparseable_year} | "
        "Dropped (outside year range): {dropped_outside_year_range} | "
        "Dropped (current year): {dropped_current_year} | "
        "Dropped (review papers): {dropped_review_papers} | "
        "Final analyzed: {final_analyzed_rows}".format(**drop_stats)
    )

    # --- Unique MeSH term counting ---
    unique_terms = set()
    for entry in df["mesh_terms"].dropna():
        terms = [term.split("','")[0].replace("('", "") for term in entry.split("),")]
        unique_terms.update(terms)

    # One colour per MeSH term for all figures of this run (most frequent terms get the strongest colours)
    term_totals = Counter(term.split("','")[0].replace("('", "") for entry in df["mesh_terms"].dropna() for term in entry.split("),"))
    plot_style.reset_term_colors([t for t, _ in term_totals.most_common(9)])

    print(f"[SUMMARY] Total papers analyzed: {drop_stats['final_analyzed_rows']}")
    print(f"[SUMMARY] Unique MeSH terms identified: {len(unique_terms)}")


    # --- Write Summary ---
    summary_path = os.path.join(output_dir, "analysis_summary.txt")

    outputs = {
        "wordcloud": bool(wordcloud),
        "cooccurance": bool(cooccurance),
        "mesh_gn": bool(mesh_gn),
        "generate_full_matrix": bool(generate_full_matrix),
        "trend_line": bool(trend_line),
        "mesh_stacked": bool(mesh_stacked),
        "journals": bool(journals),
        "authors": bool(authors),
        "institutions": bool(institutions),
        "authors_gn": bool(authors_gn),
        "institutions_gn": bool(institutions_gn),
        "histogram": bool(histogram),
        "longitudinal_wordcloud": bool(longitudinal_wordcloud),
        "use_entropy_window": bool(use_entropy_window),
    }

    extra_params = {
        "exclude_current_year": exclude_current_year,
        "exclude_review_papers": exclude_review_papers,
        "fixed_window_size": fixed_window_size,
        "entropy_min_window": entropy_min_window,
        "entropy_max_window": entropy_max_window,
        "year_range": year_range,
        "cohort_list_len": len(cohort_list) if cohort_list else 0,
        "mesh_dictionary": os.path.basename(mesh_dictionary_path) if mesh_dictionary_path else None,
    }

    append_analysis_summary(
        summary_path=summary_path,
        drop_stats=drop_stats,
        unique_mesh_terms_count=len(unique_terms),
        outputs=outputs,
        extra_params=extra_params,
    )

    print(f"[INFO] Summary appended to: {summary_path}")



    # --- Entropy Trends ---
    if (histogram or (longitudinal_wordcloud and use_entropy_window)):
        plot_entropy_trends(year_counts, output_dir, entropy_min_window, entropy_max_window)

    # --- Histogram ---
    if histogram:
        plot_histogram(
            df, year_counts, output_dir,
            title="Publications per year",
            filename="full_histogram.png"
        )

        if use_entropy_window and len(year_counts) > 0:
            from_year, to_year = compute_max_entropy_window(
                year_counts, entropy_min_window, entropy_max_window
            )
            subset = df[(df["year"] >= from_year) & (df["year"] <= to_year)]
            subset_counts = subset["year"].value_counts().sort_index()

            plot_histogram(
                subset, subset_counts, output_dir,
                title=f"Publications per year, {int(from_year)}-{int(to_year)} (most even window)",
                filename=f"histogram_{from_year}_{to_year}.png"
            )

    # --- Wordcloud ---
    if wordcloud:
        generate_wordcloud(df, output_dir)

    if longitudinal_wordcloud:
        if use_entropy_window:
            intervals = get_all_entropy_intervals(year_counts, entropy_min_window, entropy_max_window)
        elif fixed_window_size:
            year_start = int(min(year_counts.index))
            year_end = int(max(year_counts.index))
            intervals = [
                (start, min(start + fixed_window_size - 1, year_end))
                for start in range(year_start, year_end + 1, fixed_window_size)
            ]
        else:
            intervals = []

        for from_year, to_year in intervals:
            wc_df = df[(df['year'] >= from_year) & (df['year'] <= to_year)]
            generate_wordcloud(wc_df, output_dir, frequency_threshold=3, suffix=f"{from_year}_{to_year}")

    # --- Co-occurrence Matrix ---
    if cooccurance:
        if not keys_and_classes_path:
            raise ValueError("keys_and_classes_path must be provided when cooccurance is True.")
        generate_cooccurrence_matrix(df, keys_and_classes_path, mesh_dictionary_path, output_dir, generate_full_matrix,
                                     heatmap_max_categories=cooccurrence_top_categories)

    # --- Top MeSH Term Trends ---
    if trend_line:
        print(f"[INFO] Plotting top {top_trend} MeSH terms using mode: {trend_mode}")
        plot_top_terms_per_year(df, output_dir=output_dir, top_n=top_trend, mode=trend_mode)

    # --- Custom Terms (ngram-style) ---
    if ng_terms:
        print(f"[INFO] Generating trend line for custom MeSH terms: {ng_terms}")
        filename_suffix = "_".join(ng_terms[:5]) + ("_more" if len(ng_terms) > 5 else "")
        plot_custom_terms_trend(
            df,
            terms_to_track=ng_terms,
            output_dir=output_dir,
            normalize=normalize_ng,
            filename_suffix=filename_suffix
        )

    # --- Stacked Top Terms ---
    if mesh_stacked:
        print(f"[INFO] Generating stacked bar plot for top {top_stack} MeSH terms per year...")
        plot_stacked_terms_relative_per_year(df, output_dir=output_dir, top_n=top_stack, font_size=mesh_stacked_font_size, rotation=mesh_stacked_rotation)

    # --- Graph Network ---
    if mesh_gn:
        top_matrix_path = os.path.join(output_dir, f"Top_{top_mesh_gn}_MeSH_cooccurrence_matrix.xlsx")

        if not os.path.exists(top_matrix_path):
            print(f"[INFO] Generating Top {top_mesh_gn} MeSH co-occurrence matrix for graph network...")
            top_matrix_path = generate_topn_mesh_cooccurrence_matrix(
                df,
                output_dir=output_dir,
                top_n=top_mesh_gn,
                also_generate_full=generate_full_matrix
            )
        else:
            print(f"[INFO] Using existing MeSH co-occurrence matrix: {top_matrix_path}")

        generate_mesh_graph_network_plot(
            cooccur_file=top_matrix_path,
            output_dir=output_dir,
            top_n=top_mesh_gn,
            threshold_percentile=mesh_gn_percentile,
            edge_thickness_multiplier=mesh_gn_thickness,
            min_degree=mesh_gn_degree,
            label_top_n=-1 if network_labels is None else network_labels,
            n_papers=len(df),
        )

    # --- Top Journals ---

    if journals:
        print(f"[INFO] Generating Top {top_journals} Journals bar plot...")
        plot_top_journals_bar(df, output_dir=output_dir, topn=top_journals)

    # --- Top Authors ---

    if authors:
        print(f"[INFO] Generating Top {top_authors} Authors bar plot...")
        plot_top_authors_bar(df, output_dir=output_dir, topn=top_authors, first_author_only=first_author_only, include_institution_name=include_institution_name)

    # --- Top Institutions ---

    if institutions:
        print(f"[INFO] Generating Top {top_institutions} Institutions bar plot...")
        plot_top_first_author_institutions_bar(df, output_dir=output_dir, topn=top_institutions)

    # --- Authors collaboration Graph Network ---

    if authors_gn:
        print("[INFO] Generating Authors collaboration Graph Network...")
        generate_author_collab_graph_from_pipeline_df(df, output_dir=output_dir, top_n=top_authors_gn, threshold_percentile=authors_gn_percentile,
                                                      edge_thickness_multiplier=authors_gn_thickness, min_degree=authors_gn_degree,
                                                      label_top_n=30 if network_labels is None else network_labels)

    # --- Institutions collaboration Graph Network ---

    if institutions_gn:
        print("[INFO] Generating Institutions collaboration Graph Network...")
        generate_institution_collab_graph_from_pipeline_df(df, output_dir=output_dir, top_n=top_institutions_gn, threshold_percentile=institutions_gn_percentile,
                                                      edge_thickness_multiplier=institutions_gn_thickness, min_degree=institutions_gn_degree,
                                                      label_top_n=20 if network_labels is None else network_labels)

