"""
utils.py

Utility functions supporting MeSH term retrieval, configuration parsing and analysis of fetched data:
- mesh_on_demand: Fetch MeSH terms from NCBI's MOD API.
- get_mesh_tuples_from_terms: Map MOD terms to their UIDs from the MeSH dictionary.
- get_missing_info: Load missing email/API key from config.ini.
- load_excel_data: Load Excel file single (PMID text file based) or multisheet (cohort based).
"""

import pandas as pd
import numpy as np
import os
import re
import time
import logging
import requests
import json
from datetime import datetime
from scipy.stats import entropy
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator, FuncFormatter
import itertools
import difflib
from itertools import combinations
from collections import Counter, defaultdict
import networkx as nx
from adjustText import adjust_text
from wordcloud import WordCloud
from networkx.algorithms import community
from meshlytics import plot_style as ps

#--------------------- Dependency check -------------------------
# ensure_required_libraries() moved to dependencies.py (top level) so it can run before tools/ imports.


# ----- MeSH tree index (all tree positions of every descriptor) -----

_MESH_INDEX_CACHE = {}

def load_mesh_index(mesh_dictionary_path):
    """
    Reads the MeSH dictionary ({L1: {L2: {L3: {UID: term}}}}) once and returns
    {UID: {"term": name, "paths": [(L1, L2, L3 or None), ...]}} with EVERY tree position of a descriptor
    (43.9% of descriptors sit in more than one level-3 node). A level-3 key equal to its level-2 key
    (e.g. C04 under C04) marks a descriptor whose own tree number is a level-2 code (e.g. Neoplasms);
    its level 3 is returned as None.
    """
    key = os.path.abspath(mesh_dictionary_path)
    if key in _MESH_INDEX_CACHE:
        return _MESH_INDEX_CACHE[key]
    with open(mesh_dictionary_path, "r", encoding="utf-8") as f:
        mesh_dictionary = json.load(f)
    index = {}
    for l1, d1 in mesh_dictionary.items():
        for l2, d2 in d1.items():
            for l3, d3 in d2.items():
                level3 = None if l3 == l2 else l3
                for uid, term in d3.items():
                    entry = index.setdefault(uid, {"term": term, "paths": []})
                    if (l1, l2, level3) not in entry["paths"]:
                        entry["paths"].append((l1, l2, level3))
    for entry in index.values():
        entry["paths"].sort()
    _MESH_INDEX_CACHE[key] = index
    return index


def format_mesh_path(path, keys_and_classes):
    """('A', 'A08', 'A08.186') -> 'A (Anatomy) > A08 (Nervous System) > A08.186 (Central Nervous System)'."""
    return " > ".join(f"{k} ({keys_and_classes.get(k, 'Unknown')})" for k in path if k)


def get_mesh_tuples_from_terms(terms, file_path):
    """
    Generates mesh tuples in format (mesh_term,UID) for mesh_terms obtained by mesh_on_demand function
    """

    with open(file_path, 'r') as file:
        mesh_dict = json.load(file)

    # getting uid for mesh tuple
    def find_uid_for_mesh_term(d, mesh_term):
        for key, value in d.items():
            if isinstance(value, dict):
                result = find_uid_for_mesh_term(value, mesh_term)
                if result:
                    return result
            elif value == mesh_term:
                return key
        return None

    mesh_tuples = []
    for term in terms:
        uid = find_uid_for_mesh_term(mesh_dict, term)
        if uid:
            mesh_tuples.append((term, uid))

    # Convert the list of tuples into the desired string format
    mesh_term_count = len(mesh_tuples)
    mesh_terms = ','.join(f"('{term}','{uid}')" for term, uid in mesh_tuples)

    return mesh_term_count, mesh_terms


#------------------- Adding citation count to first sheet (Cohorts .xlsx file) -----------------------


def update_cohort_citation_counts(excel_file_path):
    """
    Updates the first sheet of the given Excel file by adding:
      1) total_citations
      2) total_citations_excluding_review_papers

    The function reads cohort names from the first column of the first sheet,
    counts rows in corresponding cohort sheets, and saves the updated data
    back to the SAME Excel file.
    """

    # Load Excel file
    excel_data = pd.ExcelFile(excel_file_path)

    # Read the first sheet (cohorts sheet)
    first_sheet_name = excel_data.sheet_names[0]
    first_sheet_df = pd.read_excel(excel_data, sheet_name=first_sheet_name)

    # Extract cohort names from the first column
    cohort_names = first_sheet_df.iloc[:, 0].dropna().tolist()

    total_citations = []
    total_citations_excluding_reviews = []

    # Process each cohort sheet
    for cohort in cohort_names:
        try:
            cohort_df = pd.read_excel(excel_data, sheet_name=cohort)

            # Total citations = number of rows
            total_citations.append(len(cohort_df))

            # Citations excluding review papers
            if 'review_paper' in cohort_df.columns:
                non_review_count = len(
                    cohort_df[cohort_df['review_paper'] == 'No']
                )
            else:
                non_review_count = 0

            total_citations_excluding_reviews.append(non_review_count)

        except Exception:
            # Sheet missing, empty, or unreadable
            total_citations.append(0)
            total_citations_excluding_reviews.append(0)

    # Add/update columns in the first sheet
    first_sheet_df['total_citations'] = total_citations
    first_sheet_df['total_citations_excluding_review_papers'] = total_citations_excluding_reviews

    # Write back to the SAME file
    with pd.ExcelWriter(excel_file_path, engine='openpyxl', mode='w') as writer:
        # Write updated first sheet
        first_sheet_df.to_excel(writer, sheet_name=first_sheet_name, index=False)

        # Write remaining sheets unchanged
        for sheet_name in excel_data.sheet_names[1:]:
            df = pd.read_excel(excel_data, sheet_name=sheet_name)
            df.to_excel(writer, sheet_name=sheet_name, index=False)

    print("Updated first sheet with citations count.")




#-------------------------- Mesh On Demand - NCBI --------------------------------

def mesh_on_demand(abstract_text, log_func=print, contact_email=None):
    """
    Gets MeSH terms for an abstract from NLM MeSH on Demand (experimental; used only with fetch --mesh-on-demand).
    log_func receives the status/retry messages (default: print; fetch passes its log file).
    contact_email is added to the User-Agent so NLM can contact the user.
    Returns a list of MeSH terms, or 'manual_check' if the terms could not be retrieved.
    """

    # NLM MeSH on Demand endpoint used by its web page (no published API; may change or reject requests).
    url = 'https://meshb.nlm.nih.gov/api/MOD'

    data = {
        "input": abstract_text
    }

    # Requests identify Meshlytics (and the user's contact email, if given) instead of imitating a browser.
    from meshlytics import __version__
    agent = f"Meshlytics/{__version__} (MeSH analysis research tool"
    agent += f"; mailto:{contact_email})" if contact_email else ")"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": agent,
        "Accept": "*/*",
    }

    # Retry parameters
    max_retries = 2# Maximum number of retries
    base_wait = 5  # seconds
    max_wait = 30  # cap wait time


    # Looping for retry mechanism
    for attempt in range(max_retries):
        try:
            # Sends the POST request
            response = requests.post(url, data=json.dumps(data), headers=headers, timeout=140) #80 seconds timeout, usually it take 30-35 seconds

            # Checking for a successful response
            if response.status_code == 200:
                # Markers for isolating the section with the MeSH terms
                start_marker = "-- MeSH Terms --"
                end_marker = "--"

                # Ensuring the markers are in the response text
                if start_marker in response.text and end_marker in response.text:
                    # Extract the section between the markers
                    mesh_section = response.text.split(start_marker)[1].split(end_marker)[0]
                    mesh_terms = [term.strip() for term in mesh_section.split("\n") if term.strip()]
                    return mesh_terms
                else:
                    log_func("Error: MeSH Terms section not found in response.")
                    return 'manual_check'

            elif response.status_code == 500:
                return 'manual_check'

            else:
                log_func(f"Attempt {attempt + 1} of {max_retries} - Error: Received status code {response.status_code}")
                wait_time = min(base_wait * (2 ** attempt), max_wait)
                time.sleep(wait_time)

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            log_func(f"Attempt {attempt + 1} of {max_retries} - Connection error")
            wait_time = min(base_wait * (2 ** attempt), max_wait)
            time.sleep(wait_time)


    # If max retries are exceeded
    log_func("Max retries exceeded. Unable to connect to the server or a bad request was made (likely unavailable abstract in entrez database).")
    return "manual_check"


# -------------------------- Helper Setup for analysis --------------------------

def create_output_dir(base='Analysis_Results', data_file=None):
    """
    Creates the analysis output folder.

    With data_file (used by analyze): a new folder per run beside the data file,
        <data folder>/Analysis_<data file name, max 40 chars, without '_pmids_fetched_data'>_<MonDD_YYYY>_<HHMMSS>
    so results stay with their dataset and runs never mix or overwrite each other.
    Without data_file: '<base>_<MonDD_YYYY>' in the current folder (previous behaviour).
    """
    if data_file:
        data_dir = os.path.dirname(os.path.abspath(data_file))
        stem = os.path.splitext(os.path.basename(data_file))[0]
        stem = re.sub(r"_pmids_fetched_data|_fetched_data", "", stem)  # redundant inside its own data folder
        stem = re.sub(r"[^A-Za-z0-9_\-]+", "_", stem)[:40].strip("_")
        stamp = datetime.now().strftime("%b%d_%Y_%H%M%S")  # Example: Nov09_2026_143205
        folder = os.path.join(data_dir, f"Analysis_{stem}_{stamp}")
        # Windows limits paths to 260 characters unless long paths are enabled; result files add ~50
        if os.name == "nt" and len(os.path.abspath(folder)) > 200:
            print(f"[WARN] The analysis folder path is {len(os.path.abspath(folder))} characters long; some result files may exceed "
                  f"the Windows 260-character path limit. Move the data to a shorter folder path if saving fails.")
    else:
        today = datetime.today().strftime("%b%d_%Y")  # Example: May16_2025
        folder = f"{base}_{today}"
    os.makedirs(folder, exist_ok=True)
    return folder


# ---------------------- Input data file lookup ----------------------

SEARCH_SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "essentials", "node_modules", "tools"}


def _iter_files(roots, extensions, max_depth=4):
    """Yields file paths under roots (up to max_depth folder levels), skipping code/backup/analysis folders."""
    seen_dirs = set()
    for search_root in roots:
        search_root = os.path.abspath(search_root)
        if not os.path.isdir(search_root):
            continue
        base_depth = search_root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(search_root):
            real = os.path.normcase(os.path.abspath(dirpath))
            if real in seen_dirs:
                dirnames[:] = []
                continue
            seen_dirs.add(real)
            if dirpath.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames
                           if d not in SEARCH_SKIP_DIRS and not d.startswith(("_backup", "Analysis_", "."))]
            for f in filenames:
                if f.lower().endswith(extensions) and not f.startswith("~$"):
                    yield os.path.join(dirpath, f)


def print_file_candidates(paths, scores=None):
    """Numbered list of candidate files: name, modified time, folder."""
    print("\n  #   Match     File name                                               Modified          Folder")
    print("  " + "-" * 140)
    for i, path in enumerate(paths, start=1):
        match = "exact" if scores is None else f"{scores[i - 1]:.2f}"
        modified = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
        print(f"  {i:<3} {match:<9} {os.path.basename(path)[:55]:<55} {modified:<17} {os.path.dirname(path)}")
    print()


def resolve_input_file(file_arg, extensions=(".xlsx",), search_roots=None, max_depth=4, input_func=input, label="data file"):
    """
    Resolves the -f input file of a command.

    - Existing path: used as given.
    - Otherwise the file NAME (with or without extension) is searched in search_roots
      (default: the current folder, up to max_depth levels deep,
      e.g. inside TopCited_* output folders):
        * one exact match       -> used
        * several exact matches -> numbered list (newest first), user selects (Enter = newest)
        * no exact match        -> up to 5 similar file names offered (spelling errors)
    The resolved absolute path is printed so the user can confirm it is the intended file.

    Returns:
        absolute path of the file to use
    Raises:
        FileNotFoundError if nothing suitable is found or the user quits
    """
    def report(path, how):
        modified = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
        print(f"[INFO] Using {label} ({how}): {os.path.basename(path)}")
        print(f"[INFO]   Location: {os.path.dirname(path)}   (modified {modified})")
        return path

    def choose(paths, scores=None):
        print_file_candidates(paths, scores)
        while True:
            try:
                answer = input_func(f"Select {label} [1-{len(paths)}] (Enter = 1), 'q' to quit: ").strip().lower()
            except EOFError:
                raise FileNotFoundError(f"Several files match '{file_arg}'; pass the full path with -f.") from None
            if answer in ("q", "quit"):
                raise FileNotFoundError(f"{label.capitalize()} selection cancelled by user.")
            if answer == "":
                answer = "1"
            if answer.isdigit() and 1 <= int(answer) <= len(paths):
                return paths[int(answer) - 1]
            print(f"[INFO] '{answer}' is not a valid choice.")

    # --- 1. Path as given (also try adding the extension) ---
    candidates_direct = [file_arg] + [file_arg + ext for ext in extensions if not file_arg.lower().endswith(extensions)]
    for cand in candidates_direct:
        if os.path.isfile(cand):
            return report(os.path.abspath(cand), "path as given")

    # --- 2. Search by file name ---
    name = os.path.basename(file_arg.rstrip("\\/"))
    targets = {name.lower()} if name.lower().endswith(extensions) else {(name + ext).lower() for ext in extensions}
    if search_roots is None:
        search_roots = [os.getcwd()]
    print(f"[INFO] '{file_arg}' not found at the given path; searching by file name in: {', '.join(search_roots)} (up to {max_depth} folder levels)")

    all_files = list(_iter_files(search_roots, extensions, max_depth=max_depth))
    exact = sorted({os.path.abspath(f) for f in all_files if os.path.basename(f).lower() in targets},
                   key=os.path.getmtime, reverse=True)

    if len(exact) == 1:
        return report(exact[0], "found by name")
    if len(exact) > 1:
        print(f"[INFO] {len(exact)} files named '{os.path.basename(exact[0])}' found (newest first):")
        return report(choose(exact), "selected")

    # --- 3. Similar names (spelling errors) ---
    query = os.path.splitext(name)[0].lower()
    scored = []
    for f in {os.path.abspath(x) for x in all_files}:
        ratio = difflib.SequenceMatcher(None, query, os.path.splitext(os.path.basename(f))[0].lower()).ratio()
        if ratio >= 0.6:
            scored.append((ratio, os.path.getmtime(f), f))
    scored.sort(reverse=True)
    scored = scored[:5]
    if scored:
        print(f"[INFO] No file named '{name}' found. Closest file names:")
        return report(choose([f for _, _, f in scored], [r for r, _, _ in scored]), "selected")

    raise FileNotFoundError(
        f"Input file not found: {file_arg}\n"
        f"  Searched by name in: {', '.join(search_roots)} (up to {max_depth} folder levels).\n"
        f"  Pass the full path with -f, or run the command from the folder that contains the file."
    )


# ---------------------- Keyword file (MeSH counts) helpers ----------------------

KEYWORD_FILE_BASENAME = "mesh_terms_with_keys_and_counts"
FILTER_YES_VALUES = {'yes', 'y', '1', 'true'}  # compared in lower case


def is_edited_keyword_file(path):
    """True if the keyword file has at least one term marked in 'filter_out_term?' (yes/y/1/true)."""
    try:
        df = pd.read_excel(path, usecols=lambda c: c == 'filter_out_term?')
    except Exception:
        return False
    if 'filter_out_term?' not in df.columns:
        return False
    return df['filter_out_term?'].astype(str).str.strip().str.lower().isin(FILTER_YES_VALUES).any()


def get_safe_keyword_file_path(output_dir):
    """
    Path for a new MeSH counts / keyword file in output_dir.
    Never overwrites a keyword file the user has edited (terms marked in 'filter_out_term?'):
    in that case a dated copy name is returned instead.
    """
    default_path = os.path.join(output_dir, f"{KEYWORD_FILE_BASENAME}.xlsx")
    if os.path.exists(default_path) and is_edited_keyword_file(default_path):
        stamp = datetime.now().strftime("%b%d_%Y_%H%M%S")
        new_path = os.path.join(output_dir, f"{KEYWORD_FILE_BASENAME}_{stamp}.xlsx")
        print(f"[INFO] {default_path} has terms marked in 'filter_out_term?' and is kept unchanged.")
        print(f"[INFO] New MeSH counts are saved as: {new_path}")
        return new_path
    return default_path


def find_keyword_file(data_file):
    """
    Finds the keyword file (mesh_terms_with_keys_and_counts*.xlsx) beside the data file.
    Preference: files with terms marked in 'filter_out_term?', then the most recently modified.

    Returns:
        (path or None, list of other candidate paths)
    """
    data_dir = os.path.dirname(os.path.abspath(data_file))
    candidates = [
        os.path.join(data_dir, f) for f in os.listdir(data_dir)
        if f.startswith(KEYWORD_FILE_BASENAME) and f.lower().endswith(".xlsx") and not f.startswith("~$")
    ]
    if not candidates:
        return None, []
    ranked = sorted(candidates, key=lambda c: (is_edited_keyword_file(c), os.path.getmtime(c)), reverse=True)
    return ranked[0], ranked[1:]


# ---------------------- Fetch log file ----------------------

def get_fetch_logger(log_path):
    """
    Logger that writes timestamped per-request lines to log_path only (not to the console),
    so the console can show progress bars while the full request timing is kept in the log.
    """
    logger = logging.getLogger(f"meshlytics.fetch.{os.path.abspath(log_path)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(file_handler)
    return logger


def close_fetch_logger(logger):
    """Closes the log file handlers of a fetch logger."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

def load_excel_data(excel_file, cohort_list=None):
    xls = pd.ExcelFile(excel_file)
    sheet_names = xls.sheet_names

    if len(sheet_names) == 1:
        # Single-sheet file from PubMed ID list
        print(f"[INFO] Analyzing data from single-sheet file: {sheet_names[0]}")
        data_sheets = sheet_names

    else:
        # Multi-cohort file: skip first sheet
        all_data_sheets = sheet_names[1:]

        if cohort_list:
            cohort_list_upper = [c.upper().strip() for c in cohort_list]
            normalized_sheet_map = {s.upper().strip(): s for s in all_data_sheets}
            matched_sheets = [normalized_sheet_map[s] for s in cohort_list_upper if s in normalized_sheet_map]
            if not matched_sheets:
                raise ValueError("None of the cohort names matched any sheet in the Excel file.")
            print(f"[INFO] Analyzing data from {len(matched_sheets)} cohorts: {', '.join(matched_sheets)}")
            data_sheets = matched_sheets
        else:
            print(f"[INFO] Analyzing data from all {len(all_data_sheets)} cohorts (excluding metadata sheet)")
            data_sheets = all_data_sheets

    return pd.concat([pd.read_excel(xls, sheet_name=sheet) for sheet in data_sheets], ignore_index=True)


def append_analysis_summary(
    summary_path: str,
    drop_stats: dict,
    unique_mesh_terms_count: int,
    outputs: dict,
    extra_params: dict | None = None,
):
    """
    Append (or create) a run-stamped analysis summary block.

    Args:
        summary_path: path to analysis_summary.txt
        drop_stats: dict with loaded/dropped/final counts
        unique_mesh_terms_count: int
        outputs: dict of output flags, e.g. {"wordcloud": True, "cooccurance": False, ...}
        extra_params: optional dict for run context (year_range, entropy windows, etc.)
    """
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    mode = "a" if os.path.exists(summary_path) else "w"
    with open(summary_path, mode) as f:
        if mode == "a":
            f.write("\n" + ("=" * 80) + "\n")

        f.write(f"RUN @ {ts}\n")

        # --- Filtering summary ---
        f.write("\n=== INPUT / FILTERING SUMMARY ===\n")
        f.write(f"Rows loaded: {drop_stats.get('loaded_rows', 'NA')}\n")
        f.write(f"Dropped (unparseable/missing year): {drop_stats.get('dropped_unparseable_year', 'NA')}\n")
        f.write(f"Dropped (outside year range): {drop_stats.get('dropped_outside_year_range', 'NA')}\n")
        f.write(f"Dropped (current year): {drop_stats.get('dropped_current_year', 'NA')}\n")
        f.write(f"Dropped (review papers): {drop_stats.get('dropped_review_papers', 'NA')}\n")
        f.write(f"Final papers analyzed: {drop_stats.get('final_analyzed_rows', 'NA')}\n")
        f.write(f"Unique MeSH terms identified: {unique_mesh_terms_count}\n")

        # --- Optional run context ---
        if extra_params:
            f.write("\n=== RUN PARAMETERS ===\n")
            for k, v in extra_params.items():
                f.write(f"{k}: {v}\n")

        # --- Outputs ---
        f.write("\n=== OUTPUTS GENERATED THIS RUN ===\n")
        for k, v in outputs.items():
            f.write(f"{k}: {'Yes' if v else 'No'}\n")


# ------------------------------- Histogram -----------------------------

def plot_histogram(df, year_counts, output_dir, title, filename):
    """
    Publications per year (bar chart with the count on each bar).
    The current calendar year is drawn lighter because it is incomplete.
    """
    years = [int(y) for y in year_counts.index]
    counts = [int(c) for c in year_counts.values]
    n_years = max(years) - min(years) + 1
    fig, ax = plt.subplots(figsize=(min(16, max(7.5, 0.42 * n_years + 2.5)), 4.6))

    current_year = datetime.now().year
    colors = [ps.PRIMARY_LIGHT if y == current_year else ps.PRIMARY for y in years]
    bars = ax.bar(years, counts, width=0.72, color=colors, edgecolor="white", linewidth=0.6)
    many = n_years > 30          # long spans: vertical count labels, a year tick every 5 years
    ax.bar_label(bars, labels=[ps.fmt_count(c) for c in counts], padding=2, rotation=90 if many else 0,
                 fontsize=ps.font_size("small") * (0.85 if many else 1.0), color=ps.TEXT)

    if many:
        first = min(years) - min(years) % 5
        ax.set_xticks(np.arange(first, max(years) + 1, 5))
        ax.set_xlim(min(years) - 0.8, max(years) + 0.8)
    else:
        ax.set_xticks(np.arange(min(years), max(years) + 1, 1))
        ax.tick_params(axis="x", labelrotation=90 if n_years > 20 else (45 if n_years > 12 else 0))
    ax.set_ylim(0, max(counts) * (1.18 if many else 1.1) + 0.5)   # headroom for the tallest label
    ax.margins(x=0.01)
    ps.style_axes(ax, integer_y=True)
    ax.yaxis.set_major_formatter(ps.COUNT_FORMATTER)
    ax.set_xlabel("Publication year")
    ax.set_ylabel("Papers")

    subtitle = f"n = {sum(counts):,} papers | {min(years)}-{max(years)}"
    if current_year in years:
        subtitle += f" | {current_year} is incomplete (lighter bar)"
    ps.set_title(ax, title, subtitle)
    ps.save_figure(fig, os.path.join(output_dir, filename), message="Histogram")



# ------------------------- WordCloud Generator --------------------------
def generate_wordcloud(df, output_dir, frequency_threshold=5, suffix=""):
    """
    Wordcloud of MeSH terms (word size = number of papers tagged with the term), multi-colour (viridis) words.
    The title states the period: the window given by suffix ("2015_2020"), otherwise the first and last
    publication year of the papers in df.
    """
    from matplotlib import font_manager

    def extract_terms(col):
        terms = []
        for entry in col.dropna():
            terms.extend([term.split("','")[0].replace("('", "") for term in entry.split("),")])
        return terms

    mesh_terms = extract_terms(df['mesh_terms'])
    term_counts = Counter(mesh_terms)
    filtered = {term: freq for term, freq in term_counts.items() if freq >= frequency_threshold}
    if not filtered:
        print(f"[INFO] Wordcloud {suffix or 'for all papers'} skipped: no term reaches {frequency_threshold} papers.")
        return

    font_path = font_manager.findfont(font_manager.FontProperties(family=plt.rcParams["font.sans-serif"][0]))
    wc = WordCloud(width=3200, height=1700, background_color='white', random_state=42, font_path=font_path,
                   colormap="viridis", prefer_horizontal=0.95, relative_scaling=0.5,
                   max_words=150, margin=6).generate_from_frequencies(filtered)

    fig, ax = plt.subplots(figsize=(12, 6.6))
    ax.imshow(wc, interpolation='bilinear')
    ax.set_axis_off()
    if suffix:
        period = suffix.replace("_", "-")
    else:
        years = pd.to_numeric(df["year"], errors="coerce").dropna() if "year" in df.columns else pd.Series(dtype=float)
        if years.empty:
            period = "all years"
        elif int(years.min()) == int(years.max()):
            period = f"{int(years.min())}"
        else:
            period = f"{int(years.min())}-{int(years.max())}"
    ps.set_title(ax, f"Most frequent MeSH terms, {period}",
                 f"Word size = number of papers tagged with the term | terms in \u2265 {frequency_threshold} papers "
                 f"({len(filtered):,} terms) | n = {len(df):,} papers")
    path = os.path.join(output_dir, f"Wordcloud_{suffix}_freq_ge_{frequency_threshold}.png")
    ps.save_figure(fig, path, message="Wordcloud")



# -------------------------- Entropy Window Utilities --------------------------

def compute_max_entropy_window(year_counts, min_window=3, max_window=6):
    years = pd.to_numeric(year_counts.index).tolist()
    best_entropy = -1
    best_window = (years[0], years[-1])

    for window_size in range(min_window, max_window + 1):
        for i in range(len(years) - window_size + 1):
            window_years = years[i:i + window_size]
            values = year_counts[window_years].values.astype(float)
            prob = values / values.sum()
            ent = entropy(prob)
            if ent > best_entropy:
                best_entropy = ent
                best_window = (window_years[0], window_years[-1])
    return best_window


def get_all_entropy_intervals(year_counts, min_window=3, max_window=6, max_total_windows=5):
    years = pd.to_numeric(year_counts.index).tolist()
    selected_windows = []
    used_years = set()

    for _ in range(max_total_windows):
        best_entropy = -1
        best_window = None

        for window_size in range(min_window, max_window + 1):
            for i in range(len(years) - window_size + 1):
                window_years = tuple(years[i:i + window_size])
                if any(y in used_years for y in window_years):
                    continue
                values = year_counts[list(window_years)].values.astype(float)
                prob = values / values.sum()
                ent = entropy(prob)
                if ent > best_entropy:
                    best_entropy = ent
                    best_window = window_years

        if best_window:
            selected_windows.append((best_window[0], best_window[-1]))
            used_years.update(best_window)
        else:
            break

    return selected_windows


def plot_entropy_trends(year_counts, output_dir, min_window=3, max_window=6):
    # --- Normalize index to int and sort ---
    try:
        yc = year_counts.copy()
        yc.index = pd.to_numeric(yc.index, errors="coerce").astype("Int64")
        yc = yc.dropna()
        yc = yc.sort_index()
    except Exception as e:
        print(f"[WARN] Could not normalize year_counts index for entropy trends: {e}")
        return

    years = yc.index.astype(int).tolist()

    # --- Guard: not enough years for min_window ---
    if len(years) < min_window:
        print(
            f"[INFO] Skipping entropy trends: only {len(years)} unique years "
            f"available, but min_window={min_window}."
        )
        return

    entropy_log = []

    for window_size in range(min_window, max_window + 1):
        if len(years) < window_size:
            continue

        for i in range(len(years) - window_size + 1):
            window_years = years[i : i + window_size]

            # safe selection now that index is int-sorted
            values = yc.loc[window_years].values.astype(float)

            total = values.sum()
            if total <= 0:
                # avoid divide-by-zero / invalid entropy
                continue

            prob = values / total
            ent = entropy(prob)

            entropy_log.append(
                {
                    "start": window_years[0],
                    "end": window_years[-1],
                    "window_size": window_size,
                    "entropy": float(ent),
                }
            )

    if not entropy_log:
        print(
            f"[INFO] Skipping entropy trends: no valid windows produced "
            f"(min_window={min_window}, max_window={max_window})."
        )
        return

    entropy_df = pd.DataFrame(entropy_log)

    sizes = sorted(entropy_df["window_size"].unique())
    fig, ax = plt.subplots(figsize=(9, 4.6))
    series = []
    for i, size in enumerate(sizes):
        subset = entropy_df[entropy_df["window_size"] == size].sort_values("start")
        color = ps.SEQUENTIAL(0.35 + 0.6 * i / max(1, len(sizes) - 1))   # ordinal: light = short window
        ax.plot(subset["start"], subset["entropy"], marker="o", ms=4, lw=1.8, color=color)
        series.append((f"{size}-year window", subset["entropy"].iloc[-1], color, subset["start"].iloc[-1]))

    # Whole years on the x axis (every year when the span is short, otherwise integer ticks only)
    start_years = sorted(entropy_df["start"].unique())
    if start_years[-1] - start_years[0] <= 15:
        ax.set_xticks(start_years)
        ax.tick_params(axis="x", labelrotation=45 if len(start_years) > 12 else 0)
    else:   # long or gappy spans: about 10 whole-year ticks, evenly spaced in time
        ticks = MaxNLocator(nbins=10, integer=True, steps=[1, 2, 5, 10]).tick_values(start_years[0], start_years[-1])
        ax.set_xticks([t for t in ticks if start_years[0] <= t <= start_years[-1]])
    ps.style_axes(ax, grid="y")
    ax.margins(x=0.03, y=0.08)
    ax.set_xlabel("First year of the window")
    ax.set_ylabel("Entropy of yearly counts")
    ps.end_of_line_labels(ax, series)
    ps.set_title(ax, "How evenly publications are spread over time",
                 "Shannon entropy of publication counts in each sliding window; higher = more even spread")
    path = os.path.join(output_dir, "entropy_trends_by_year_window.png")
    ps.save_figure(fig, path, message="Entropy trend plot")


# ------------------------ Top MeSH Co-occurance Matrix -----------------------


def parse_mesh_terms(term_str):
    """
    Parses a string like:
    "('Term A','D000001'),('Term B, extra','D000002')"
    into a list of (term, UID) tuples.
    """
    if pd.isna(term_str) or not isinstance(term_str, str):
        return []

    pattern = r"\('((?:[^']|\\')+?)','(D\d+?)'\)"
    return re.findall(pattern, term_str)


def generate_topn_mesh_cooccurrence_matrix(df, output_dir, top_n=200, also_generate_full=False):
    """
    Generate MeSH term co-occurrence matrices and save them to Excel.
    """
    mesh_series = df['mesh_terms'].dropna().apply(parse_mesh_terms)

    pair_counts = Counter()
    for terms in mesh_series:
        unique_terms = list(set(t[0] for t in terms))
        for a, b in combinations(sorted(unique_terms), 2):
            pair_counts[(a, b)] += 1

    term_set = set([t for pair in pair_counts for t in pair])
    term_list = sorted(term_set)
    matrix = pd.DataFrame(0, index=term_list, columns=term_list, dtype=int)

    for (a, b), count in pair_counts.items():
        matrix.loc[a, b] = matrix.loc[b, a] = count

    if also_generate_full:
        full_path = os.path.join(output_dir, "Full_MeSH_Cooccurrence_Matrix.xlsx")
        matrix.to_excel(full_path)
        print(f"[INFO] Full co-occurrence matrix saved: {full_path}")

    top_terms = matrix.sum(axis=1).nlargest(top_n).index.tolist()
    top_matrix = matrix.loc[top_terms, top_terms]
    top_path = os.path.join(output_dir, f"Top_{top_n}_MeSH_terms_cooccurrence_matrix.xlsx")
    top_matrix.to_excel(top_path)
    print(f"[INFO] Top {top_n} MeSH co-occurrence matrix saved: {top_path}")

    return top_path

# --------------------- MeSH Co-occurrence Matrix Generator -------------------

def generate_cooccurrence_matrix(
    df,
    keys_and_classes_path,
    mesh_dictionary_path,
    output_dir,
    generate_full_matrix=False,
    heatmap_max_categories=40,
):
    import os
    import re
    import json
    import numpy as np
    import pandas as pd
    import matplotlib.pyplot as plt
    from itertools import combinations

    os.makedirs(output_dir, exist_ok=True)

    with open(keys_and_classes_path, "r") as f:
        keys_and_classes = json.load(f)

    # every tree position of every descriptor (see load_mesh_index)
    mesh_index = load_mesh_index(mesh_dictionary_path)

    # ------------------------------------------------------------
    # Save CSV with full UID-Term-Path (all tree paths per UID, ' | ' separated)
    # ------------------------------------------------------------
    terms_records = [
        {"UID": uid, "Term": e["term"], "Path": " | ".join(format_mesh_path(p, keys_and_classes) for p in e["paths"]),
         "n_tree_positions": len(e["paths"])}
        for uid, e in mesh_index.items()
    ]

    terms_df_full = pd.DataFrame(terms_records)

    if not terms_df_full.empty:
        terms_df_full.sort_values(by="UID").to_csv(
            os.path.join(output_dir, "UID_MeSH_term_path_with_levels.csv"),
            index=False
        )

    # ------------------------------------------------------------
    # Extract UIDs from dataframe
    # ------------------------------------------------------------
    def extract_uids(mesh_terms_str):
        if pd.isna(mesh_terms_str):
            return []

        # Matches 'Dxxxxx' style UIDs
        uids = re.findall(r"'D\d+'", str(mesh_terms_str))
        return [uid.replace("'", "") for uid in uids]

    df = df.copy()
    df["uids"] = df["mesh_terms"].apply(extract_uids)

    df["uid_pairs"] = df["uids"].apply(
        lambda u: list(itertools.combinations(sorted(set(u)), 2)) if u else []
    )

    # ------------------------------------------------------------
    # Identify keys per level
    # ------------------------------------------------------------
    level1_keys = [k for k in keys_and_classes if len(k) == 1]
    level2_keys = [k for k in keys_and_classes if len(k) == 3]
    level3_keys = [k for k in keys_and_classes if len(k) == 7]  # e.g., D02.355

    # Initialize square matrices with hierarchy keys first
    level1_df = pd.DataFrame(0, index=level1_keys, columns=level1_keys)
    level2_df = pd.DataFrame(0, index=level2_keys, columns=level2_keys)
    level3_df = pd.DataFrame(0, index=level3_keys, columns=level3_keys)

    # ------------------------------------------------------------
    # Populate matrices using ALL tree positions of both descriptors.
    # A pair of descriptors adds 1 to each distinct category pair it links, counted once per descriptor pair;
    # as before, only positions in different level-1 categories are counted (within-category pairs are not).
    # ------------------------------------------------------------
    l1_set, l2_set, l3_set = set(level1_keys), set(level2_keys), set(level3_keys)

    def add_pairs(matrix, pairs):
        for a, b in pairs:
            matrix.at[a, b] += 1
            matrix.at[b, a] += 1

    for pairs in df["uid_pairs"]:
        for key1, key2 in pairs:
            e1 = mesh_index.get(key1)
            e2 = mesh_index.get(key2)

            if not e1 or not e2:
                continue

            p1_pairs, p2_pairs, p3_pairs = set(), set(), set()
            for p in e1["paths"]:
                for q in e2["paths"]:
                    if p[0] == q[0]:
                        continue
                    if p[0] in l1_set and q[0] in l1_set:
                        p1_pairs.add(tuple(sorted((p[0], q[0]))))
                    if p[1] in l2_set and q[1] in l2_set:
                        p2_pairs.add(tuple(sorted((p[1], q[1]))))
                    if p[2] and q[2] and p[2] in l3_set and q[2] in l3_set:
                        p3_pairs.add(tuple(sorted((p[2], q[2]))))

            add_pairs(level1_df, p1_pairs)
            add_pairs(level2_df, p2_pairs)
            add_pairs(level3_df, p3_pairs)

    # ------------------------------------------------------------
    # Human-readable labels with duplicate-safe Level 3 labels
    # ------------------------------------------------------------
    def get_parent_key(mesh_key):
        """
        Return the immediate upper-level parent key.

        Examples:
            D02.355 -> D02
            C10.228 -> C10
        """
        if "." in mesh_key:
            return mesh_key.rsplit(".", 1)[0]
        return None

    def build_label_map(level_keys, add_parent_for_duplicates=False):
        """
        Convert hierarchy keys to readable labels.

        For Level 3 duplicate labels:
            Absorption -> Absorption (Physiological Phenomena)

        Non-duplicates stay unchanged:
            Cell Respiration -> Cell Respiration
        """
        base_labels = {
            k: keys_and_classes.get(k, k)
            for k in level_keys
        }

        label_counts = pd.Series(list(base_labels.values())).value_counts()
        duplicate_labels = set(label_counts[label_counts > 1].index)

        label_map = {}

        for k in level_keys:
            base_label = base_labels[k]

            if add_parent_for_duplicates and base_label in duplicate_labels:
                parent_key = get_parent_key(k)
                parent_label = keys_and_classes.get(parent_key, parent_key) if parent_key else None

                if parent_label:
                    label_map[k] = f"{base_label} ({parent_label})"
                else:
                    label_map[k] = base_label
            else:
                label_map[k] = base_label

        return label_map

    level1_label_map = build_label_map(level1_keys, add_parent_for_duplicates=False)
    level2_label_map = build_label_map(level2_keys, add_parent_for_duplicates=False)

    # Important update: only Level 3 duplicate labels get Level 2 parent in brackets
    level3_label_map = build_label_map(level3_keys, add_parent_for_duplicates=True)

    level1_df = level1_df.rename(index=level1_label_map, columns=level1_label_map)
    level2_df = level2_df.rename(index=level2_label_map, columns=level2_label_map)
    level3_df = level3_df.rename(index=level3_label_map, columns=level3_label_map)

    # ------------------------------------------------------------
    # Duplicate label check
    # ------------------------------------------------------------
    def report_duplicate_labels(matrix_df, level_name):
        duplicated_index = matrix_df.index[matrix_df.index.duplicated()].unique().tolist()
        duplicated_columns = matrix_df.columns[matrix_df.columns.duplicated()].unique().tolist()

        if duplicated_index or duplicated_columns:
            print(f"[WARNING] Duplicate labels remain in {level_name}.")
            if duplicated_index:
                print(f"  Duplicate row labels, first 10: {duplicated_index[:10]}")
            if duplicated_columns:
                print(f"  Duplicate column labels, first 10: {duplicated_columns[:10]}")
        else:
            print(f"[INFO] No duplicate labels in {level_name} matrix.")

    report_duplicate_labels(level1_df, "Level 1")
    report_duplicate_labels(level2_df, "Level 2")
    report_duplicate_labels(level3_df, "Level 3")

    # ------------------------------------------------------------
    # Write CSVs
    # ------------------------------------------------------------
    level1_path = os.path.join(output_dir, "level1_co_occurrence_matrix.csv")
    level2_path = os.path.join(output_dir, "level2_co_occurrence_matrix.csv")
    level3_path = os.path.join(output_dir, "level3_co_occurrence_matrix.csv")

    level1_df.to_csv(level1_path)
    print(f"[INFO] Level 1 MeSH cooccurrence matrix saved to: {level1_path}")

    level2_df.to_csv(level2_path)
    print(f"[INFO] Level 2 MeSH cooccurrence matrix saved to: {level2_path}")

    level3_df.to_csv(level3_path)
    print(f"[INFO] Level 3 MeSH cooccurrence matrix saved to: {level3_path}")

    # ------------------------------------------------------------
    # Heatmaps
    # Level 3 can be large, so heatmap is still skipped by default
    # ------------------------------------------------------------
    def save_heatmap(mat, level, fname, max_categories=None, parent_of=None):
        """
        Lower-triangle heatmap of category co-occurrence (the matrix is symmetric and the diagonal
        is always 0 because within-category pairs are not counted). Categories with no
        co-occurrence are dropped; large matrices keep the max_categories with the highest totals.
        """
        from matplotlib.colors import PowerNorm
        mat = mat.loc[mat.sum(axis=1) > 0, mat.sum(axis=0) > 0]
        n_total = mat.shape[0]
        if n_total < 2:
            print(f"[INFO] Level {level} heatmap skipped: fewer than 2 categories co-occur.")
            return
        note = ""
        if max_categories and n_total > max_categories:
            keep = mat.sum(axis=1).nlargest(max_categories).index
            mat = mat.loc[[k for k in mat.index if k in keep], [k for k in mat.columns if k in keep]]
            note = f"top {max_categories} of {n_total} categories by total; full matrix in the CSV | "
        n = mat.shape[0]
        values = mat.values.astype(float)
        mask = np.triu(np.ones_like(values, dtype=bool))
        blank_note = ""
        if parent_of:   # pairs inside the same level-1 category are never counted -> show blank, not 0
            par = [parent_of.get(k) for k in mat.index]
            same = np.array([[par[i] is not None and par[i] == par[j] for j in range(n)] for i in range(n)])
            if (same & ~mask).any():
                blank_note = "blank cells = same top-level category (not counted) | "
            mask = mask | same
        # keep only rows / columns that have at least one visible cell (drops the empty first row and
        # last column of the triangle, and Level 2 blocks that are blank by construction)
        rows = [i for i in range(n) if not mask[i, :].all()]
        cols = [j for j in range(n) if not mask[:, j].all()]
        values, mask = values[np.ix_(rows, cols)], mask[np.ix_(rows, cols)]
        plot_vals = np.ma.masked_array(values, mask=mask)
        row_labels = [ps.wrap_label(mat.index[i], 34, 2) for i in rows]
        col_labels = [ps.wrap_label(mat.columns[j], 34, 2) for j in cols]

        cell = 0.42 if n <= 20 else 0.24
        side = min(16, max(7, n * cell + 3.5))
        fig, ax = plt.subplots(figsize=(side + 1.2, side))
        vmax = max(1, values[~mask].max()) if (~mask).any() else 1
        skewed = n > 20
        norm = PowerNorm(gamma=0.5, vmin=0, vmax=vmax) if skewed else plt.Normalize(vmin=0, vmax=vmax)
        im = ax.imshow(plot_vals, cmap=ps.SEQUENTIAL, norm=norm, aspect="equal", interpolation="nearest")
        if n <= 20:  # print every value when the matrix is small enough to read
            for i in range(len(rows)):
                for j in range(len(cols)):
                    if mask[i, j]:
                        continue
                    v = values[i, j]
                    ax.text(j, i, ps.fmt_count(v), ha="center", va="center", fontsize=ps.font_size("small") * 0.85,
                            color=ps.text_color_for(ps.SEQUENTIAL(norm(v))))
        ax.set_xticks(range(len(cols)))
        ax.set_yticks(range(len(rows)))
        ax.set_xticklabels(col_labels, rotation=90, fontsize=ps.font_size("small") if n <= 20 else ps.font_size("small") * 0.75)
        ax.set_yticklabels(row_labels, fontsize=ps.font_size("small") if n <= 20 else ps.font_size("small") * 0.75)
        for side_name in ("top", "right", "left", "bottom"):
            ax.spines[side_name].set_visible(False)
        ax.tick_params(length=0)
        cbar = fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
        cbar.set_label("Descriptor pairs in the same paper" + (" (square-root scale)" if skewed else ""), fontsize=ps.font_size("medium"))
        cbar.ax.tick_params(labelsize=ps.font_size("small"))
        if skewed:
            cbar.set_ticks(ps.nice_sqrt_ticks(vmax))
        cbar.ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: ps.fmt_count(v, digits=2)))
        cbar.outline.set_visible(False)
        ps.set_title(ax, f"MeSH categories that appear together (tree level {level})",
                     f"{note}{blank_note or 'within-category pairs not counted | '}n = {len(df):,} papers")
        ps.save_figure(fig, os.path.join(output_dir, fname), message=f"Level {level} co-occurrence heatmap")

    save_heatmap(level1_df, 1, "level1_cooccurrence_heatmap.png")
    save_heatmap(level2_df, 2, "level2_cooccurrence_heatmap.png", max_categories=heatmap_max_categories,
                 parent_of={level2_label_map.get(k, k): k[0] for k in level2_keys})

    # Level 3 has too many categories for a readable heatmap; its matrix is saved as CSV only.

    # ------------------------------------------------------------
    # Full term-by-term co-occurrence matrix (optional: analyze --generate-full-matrix)
    # ------------------------------------------------------------
    if generate_full_matrix:

        # 1) Single, precompiled pattern: ('<term>','D<digits>')
        TERM_UID_RE = re.compile(r"\('([^']+)'\s*,\s*'(D\d+)'\)")

        def extract_terms_and_uids_vectorized(df, col="mesh_terms"):
            """
            Vectorized extraction of (term, uid) tuples from df[col].

            Returns:
              - matches: DataFrame with columns ['row_id','term','uid']
              - terms_per_row: Series[index=df.index] of unique term lists per row
              - uids_per_row: Series[index=df.index] of unique UID lists per row
            """
            s = df[col].astype(str)

            matches = s.str.extractall(TERM_UID_RE)
            matches.columns = ["term", "uid"]
            matches.index.names = ["row_id", "match_id"]
            matches = matches.reset_index()

            terms_per = matches.groupby("row_id")["term"].agg(lambda x: sorted(set(x)))
            uids_per = matches.groupby("row_id")["uid"].agg(lambda x: sorted(set(x)))

            terms_per = terms_per.reindex(df.index, fill_value=[]).rename("terms")
            uids_per = uids_per.reindex(df.index, fill_value=[]).rename("uids")

            return matches, terms_per, uids_per

        def build_full_term_cooccurrence_matrix(terms_per_row, dtype="int64"):
            """
            Build a symmetric term-by-term co-occurrence matrix from a Series
            of lists of terms unique per row.
            """
            all_terms = sorted({t for terms in terms_per_row for t in terms})
            n = len(all_terms)

            if n == 0:
                return pd.DataFrame(dtype=dtype)

            idx = {t: i for i, t in enumerate(all_terms)}
            mat = np.zeros((n, n), dtype=np.int64)

            for terms in terms_per_row:
                if len(terms) < 2:
                    continue

                for a, b in combinations(terms, 2):
                    i, j = idx[a], idx[b]
                    mat[i, j] += 1
                    mat[j, i] += 1

            return pd.DataFrame(
                mat.astype(dtype),
                index=all_terms,
                columns=all_terms
            )

        _, terms_per_row, _ = extract_terms_and_uids_vectorized(df, col="mesh_terms")

        full_df = build_full_term_cooccurrence_matrix(terms_per_row)

        full_csv = os.path.join(output_dir, "Full_MeSH_term_co_occurrence_matrix.csv")
        full_df.to_csv(full_csv)

        full_excel = os.path.join(output_dir, "Full_MeSH_term_co_occurrence_matrix.xlsx")
        full_df.to_excel(full_excel)

        n = full_df.shape[0]

        print(f"[INFO] Full cooccurrence matrix with {n} terms saved to {output_dir}")
        print(f"[INFO] Full CSV saved to: {full_csv}")
        print(f"[INFO] Full Excel saved to: {full_excel}")

    return {
        "level1_path": level1_path,
        "level2_path": level2_path,
        "level3_path": level3_path
    }



# --------------------- MeSH Trend Line Plot Top n terms -----------------


def plot_top_terms_per_year(
    df,
    output_dir,
    top_n=5,
    mode='total_frequency'):  # 'yearly_presence' or 'total_frequency'
    """
    Plots the trend of top MeSH terms across years.

    Parameters:
    -----------
    df : pd.DataFrame
        Must contain 'year' and 'mesh_terms' columns.
    output_dir : str
        Directory to save the output plot.
    top_n : int
        Number of top terms to consider.
    mode : str
        - 'total_frequency': top N terms with highest total frequency across all years.
        - 'yearly_presence': top N terms that appear most frequently in per-year top lists.
    """

    df = df[['year', 'mesh_terms']].dropna()
    year_term_freq = {}

    # Parse MeSH terms and count per year
    for _, row in df.iterrows():
        year = row['year']
        terms = [term.split("','")[0].replace("('", "") for term in row['mesh_terms'].split("),")]
        if year not in year_term_freq:
            year_term_freq[year] = Counter()
        year_term_freq[year].update(terms)

    records = []
    for year in sorted(year_term_freq):
        top_terms = year_term_freq[year].most_common(top_n)
        for term, freq in top_terms:
            records.append({'year': year, 'term': term, 'frequency': freq})

    plot_df = pd.DataFrame(records)

    if mode == 'total_frequency':
        # Select top N by summed frequency across all years
        global_totals = plot_df.groupby('term')['frequency'].sum()
        global_top_terms = global_totals.nlargest(top_n).index
    elif mode == 'yearly_presence':
        # Select top N by number of appearances in yearly top-N lists
        presence = plot_df['term'].value_counts().sort_index().sort_values(ascending=False, kind="stable")
        global_top_terms = presence.head(top_n).index  # equal counts in name order
    else:
        raise ValueError("mode must be either 'total_frequency' or 'yearly_presence'.")

    # Selection of terms is unchanged; each line shows the term's count in EVERY year
    # (previously a year was skipped when the term was not in that year's own top N)
    years_all = sorted(year_term_freq)
    n_papers = len(df)

    fig, ax = plt.subplots(figsize=(10, 5))
    series = []
    for term in global_top_terms:
        values = [year_term_freq[y].get(term, 0) for y in years_all]
        color = ps.term_color(term)
        ax.plot(years_all, values, marker="o", ms=4, lw=1.8, color=color)
        series.append((ps.shorten(term, 32), values[-1], color, years_all[-1]))

    ax.set_xticks(years_all)
    ax.tick_params(axis="x", labelrotation=45 if len(years_all) > 12 else 0)
    ps.style_axes(ax, grid="y", integer_y=True)
    ax.margins(x=0.02, y=0.06)
    ax.set_xlabel("Publication year")
    ax.set_ylabel("Papers tagged with the term")
    ps.end_of_line_labels(ax, series)
    rule = "highest total frequency" if mode == "total_frequency" else f"most years in the yearly top {top_n}"
    ps.set_title(ax, "Most frequent MeSH terms over time",
                 f"Top {top_n} terms by {rule} | n = {n_papers:,} papers")

    filename = f"top_{top_n}_MeSH_terms_trend_{mode}.png"
    path = os.path.join(output_dir, filename)
    ps.save_figure(fig, path, message="Line trend plot")


# ------------------------------- MeSH Ngram -----------------------------

def plot_custom_terms_trend(df, terms_to_track, output_dir, normalize=False, filename_suffix=""):

    df = df[['year', 'mesh_terms']].dropna()
    year_term_freq = {}
    year_total_counts = {}

    for _, row in df.iterrows():
        year = row['year']
        terms = [term.split("','")[0].replace("('", "") for term in row['mesh_terms'].split("),")]
        if year not in year_term_freq:
            year_term_freq[year] = Counter()
            year_total_counts[year] = 0
        year_term_freq[year].update(terms)
        year_total_counts[year] += 1

    records = []
    for year in sorted(year_term_freq):
        for term in terms_to_track:
            freq = year_term_freq[year].get(term, 0)
            if normalize:
                freq = freq / year_total_counts[year] if year_total_counts[year] else 0
            records.append({'year': year, 'term': term, 'frequency': freq})

    plot_df = pd.DataFrame(records)

    fig, ax = plt.subplots(figsize=(10, 5))
    years_all = sorted(plot_df["year"].unique())
    series = []
    for term in terms_to_track:
        sub = plot_df[plot_df["term"] == term].sort_values("year")
        values = sub["frequency"] * (100 if normalize else 1)
        color = ps.term_color(term)
        ax.plot(sub["year"], values, marker="o", ms=4, lw=1.8, color=color)
        series.append((ps.shorten(term, 32), values.iloc[-1], color, sub["year"].iloc[-1]))

    ax.set_xticks(years_all)
    ax.tick_params(axis="x", labelrotation=45 if len(years_all) > 12 else 0)
    ps.style_axes(ax, grid="y", integer_y=not normalize)
    ax.margins(x=0.02, y=0.06)
    ax.set_xlabel("Publication year")
    ax.set_ylabel("Share of papers with the term (%)" if normalize else "Papers tagged with the term")
    ps.end_of_line_labels(ax, series)
    ps.set_title(ax, "Selected MeSH terms over time",
                 f"{'Share of each year' + chr(39) + 's papers' if normalize else 'Papers per year'} | n = {len(df):,} papers")

    safe_suffix = filename_suffix.replace(" ", "_").strip("_")
    filename = f"custom_MeSH_terms_trend_{safe_suffix}.png" if filename_suffix else "custom_MeSH_terms_trend.png"
    path = os.path.join(output_dir, filename)
    ps.save_figure(fig, path, message="Custom terms trend plot")


# --------------------------- Stacked Top MeSH ---------------------------


def plot_stacked_terms_relative_per_year(df, output_dir, top_n=5, font_size=None, rotation=0):
    """
    Plots a stacked bar chart showing the top N MeSH terms per year using relative frequency (%),
    stacking terms in ascending order per year.

    Every term has one colour (legend on the right, same colours as the trend figures).
    Segment labels are written only where they fit; font_size/rotation (--mesh-stacked-font-size,
    --mesh-stacked-rotation) override the automatic labels.
    """
    df = df[['year', 'mesh_terms']].dropna()
    df['year'] = df['year'].astype(int)

    year_term_freq = {}

    # Count MeSH terms per year
    for _, row in df.iterrows():
        year = row['year']
        terms = [term.split("','")[0].replace("('", "") for term in row['mesh_terms'].split("),")]
        if year not in year_term_freq:
            year_term_freq[year] = Counter()
        year_term_freq[year].update(terms)

    # Get top-N terms per year
    records = []
    all_terms = set()
    for year in sorted(year_term_freq):
        top_terms = year_term_freq[year].most_common(top_n)
        for term, freq in top_terms:
            records.append({'year': year, 'term': term, 'frequency': freq})
            all_terms.add(term)

    stacked_df = pd.DataFrame(records)

    # Create pivot table and normalize to % per year
    pivot_df = stacked_df.pivot_table(index='year', columns='term', values='frequency', aggfunc='sum').fillna(0)
    pivot_df = pivot_df.div(pivot_df.sum(axis=1), axis=0) * 100

    # Ensure all terms appear in all years (even if 0)
    all_terms = sorted(list(all_terms))
    pivot_df = pivot_df.reindex(columns=all_terms, fill_value=0)

    # One colour per term (ordered by total share), shared with the other MeSH figures
    totals = pivot_df.sum(axis=0).sort_values(ascending=False)
    color_map = {term: ps.term_color(term) for term in totals.index}

    n_years = len(pivot_df.index)
    fig, ax = plt.subplots(figsize=(min(16, max(7.5, 0.8 * n_years + 2.5)), 5.6))
    auto_labels = font_size is None and not rotation
    label_size = font_size if font_size is not None else ps.font_size("small") * 0.9
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    bar_width = 0.72
    ax.set_xlim(-0.6, n_years - 0.4)   # final axis range BEFORE measuring label fit
    ax.set_ylim(0, 100)

    def fits(txt, x0, y0, x1, y1):
        tb = txt.get_window_extent(renderer)
        seg = ax.transData.transform([(x0, y0), (x1, y1)])
        return tb.width <= (seg[1][0] - seg[0][0]) * 0.94 and tb.height <= (seg[1][1] - seg[0][1]) * 0.92

    for idx, year in enumerate(pivot_df.index):
        row = pivot_df.loc[year]
        ordered_terms = row.sort_values().index.tolist()  # ascending by % for this year
        cumulative = 0
        for term in ordered_terms:
            height = row[term]
            if height > 0:
                color = color_map[term]
                ax.bar(idx, height, width=bar_width, bottom=cumulative, color=color, edgecolor="white", linewidth=0.8)
                if auto_labels:
                    # label only if the text fits inside the segment (checked in screen units);
                    # try full name, wrapped name, then a smaller size (grey terms need their label most)
                    box = (idx - bar_width / 2, cumulative, idx + bar_width / 2, cumulative + height)
                    attempts = [(term, label_size), (ps.wrap_label(term, 14, 2), label_size),
                                (ps.wrap_label(term, 14, 2), max(6.5, label_size * 0.8))]
                    for text, size in attempts:
                        txt = ax.text(idx, cumulative + height / 2, text, ha="center", va="center",
                                      fontsize=size, color=ps.text_color_for(color), linespacing=0.95)
                        if fits(txt, *box):
                            break
                        txt.remove()
                elif height > 2:
                    ax.text(idx, cumulative + height / 2, term, ha="center", va="center",
                            fontsize=label_size, rotation=rotation, color=ps.text_color_for(color))
                cumulative += height

    ax.set_xticks(range(n_years))
    ax.set_xticklabels(pivot_df.index, rotation=45 if n_years > 12 else 0)
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_ylabel("Share of mentions (%)")
    ax.set_xlabel("Publication year")
    ps.style_axes(ax)

    from matplotlib.patches import Patch
    coloured = [t for t in totals.index if not ps.is_grey_term(t)]
    handles = [Patch(facecolor=color_map[t], edgecolor="none", label=ps.shorten(t, 28)) for t in coloured]
    n_grey = len(totals) - len(coloured)
    if n_grey:
        handles.append(Patch(facecolor=ps.TERM_GREYS[0], edgecolor="none", label=f"{n_grey} more term{'' if n_grey == 1 else 's'} (grey)"))
    legend_handles = handles

    # exact shares for every term and year (also identifies grey segments too small to label)
    table_path = os.path.join(output_dir, f"stacked_top{top_n}_terms_relative_per_year.xlsx")
    pivot_df.round(2).rename_axis("year").to_excel(table_path)
    ps.set_title(ax, f"Top {top_n} MeSH terms in each year",
                 f"Share of each year's top-{top_n} mentions | stacked smallest to largest | grey = other terms, named where they fit "
                 f"(all shares in the .xlsx) | n = {len(df):,} papers")
    ps.legend_below(ax, legend_handles, max_cols=6)

    path = os.path.join(output_dir, f"stacked_top{top_n}_terms_relative_sorted_per_year.png")
    ps.save_figure(fig, path, message="Sorted relative stacked bar plot")

    # companion figure: every term that enters a year's top N, readable however many there are
    plot_top_terms_year_heatmap(pivot_df, list(totals.index), color_map, output_dir, top_n, len(df))


def plot_top_terms_year_heatmap(pivot_df, ordered_terms, color_map, output_dir, top_n, n_papers):
    """
    Term x year heatmap for the stacked top-N chart (same numbers as stacked_top<N>_terms_relative_per_year.xlsx).
    One row per term that is in the top N of at least one year (ordered by total share), one column per year;
    shade = the term's share of that year's top-N mentions; blank = not in that year's top N.
    A swatch left of the term repeats its colour in the stacked chart (no swatch = grey there).
    """
    mat = pivot_df[ordered_terms].T                       # rows = terms, columns = years
    vals = mat.values.astype(float)
    masked = np.ma.masked_less_equal(vals, 0)
    n_rows, n_cols = vals.shape
    cell_w, cell_h = (0.42, 0.3) if n_cols <= 20 else (0.3, 0.26)
    fig_w = min(18, max(6.5, n_cols * cell_w + 3.6))
    fig_h = min(20, max(3.2, n_rows * cell_h + 1.9))
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    vmax = max(1.0, float(np.nanmax(vals)))
    norm = plt.Normalize(vmin=0, vmax=vmax)
    im = ax.imshow(masked, cmap=ps.SEQUENTIAL, norm=norm, aspect="auto", interpolation="nearest")
    show_values = n_cols <= 30 and n_rows <= 60
    if show_values:
        fs = ps.font_size("small") * (0.8 if n_cols > 20 else 0.9)
        for i in range(n_rows):
            for j in range(n_cols):
                if vals[i, j] > 0:
                    ax.text(j, i, f"{vals[i, j]:.0f}", ha="center", va="center", fontsize=fs,
                            color=ps.text_color_for(ps.SEQUENTIAL(norm(vals[i, j]))))
    ax.set_xticks(range(n_cols))
    ax.set_xticklabels([str(y) for y in mat.columns], rotation=90 if n_cols > 20 else (45 if n_cols > 12 else 0))
    ax.set_yticks(range(n_rows))
    ax.set_yticklabels([ps.shorten(t, 40) for t in mat.index])
    ax.tick_params(length=0)
    ax.tick_params(axis="y", pad=16)                      # room for the colour swatches
    ax.set_xticks(np.arange(-0.5, n_cols, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, n_rows, 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=1.0)
    ax.tick_params(which="minor", length=0)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    # swatch = colour of the term in the stacked chart, a fixed 8 pt left of the grid (between grid and label)
    from matplotlib.transforms import ScaledTranslation
    swatch_tr = ax.get_yaxis_transform() + ScaledTranslation(-8 / 72, 0, fig.dpi_scale_trans)
    rows_c = [i for i, t in enumerate(mat.index) if not ps.is_grey_term(t)]
    if rows_c:
        ax.scatter([0] * len(rows_c), rows_c, marker="s", s=8.5 ** 2, c=[color_map[mat.index[i]] for i in rows_c],
                   transform=swatch_tr, clip_on=False, linewidths=0, zorder=3)
    cbar = fig.colorbar(im, ax=ax, fraction=0.03 if n_rows > 15 else 0.05, pad=0.02)
    cbar.set_label(f"Share of the year's top-{top_n} mentions (%)", fontsize=ps.font_size("small"))
    cbar.ax.tick_params(labelsize=ps.font_size("small"))
    cbar.outline.set_visible(False)
    ax.set_xlabel("Publication year")
    ps.set_title(ax, f"Top {top_n} MeSH terms in each year, all {n_rows} terms",
                 f"Shade and number = share of the year's top-{top_n} mentions (%) | blank = not in that year's top {top_n} | "
                 f"swatch = colour in the stacked chart | n = {n_papers:,} papers")
    path = os.path.join(output_dir, f"top{top_n}_terms_per_year_heatmap.png")
    ps.save_figure(fig, path, message="Top terms per year heatmap")


# ------------------------------ Top Journals ----------------------------


def plot_ranked_barh(labels, values, path, title, subtitle, xlabel="Papers", label_width=44, message="Bar chart",
                     figsize=None, dpi=None):
    """
    Sorted horizontal bar chart used by the journal, author and institution rankings.
    labels/values are given largest first; the largest bar is drawn at the top.
    """
    labels = list(labels)[::-1]
    values = [int(v) for v in list(values)[::-1]]
    wrapped = [ps.wrap_label(label, label_width, 2) for label in labels]
    extra_lines = sum(w.count("\n") for w in wrapped)
    if figsize is None:
        figsize = (9, ps.bar_figure_height(len(values)) + 0.16 * extra_lines)
    fig, ax = plt.subplots(figsize=figsize)
    y = list(range(len(values)))
    bars = ax.barh(y, values, height=0.68 if len(values) > 3 else 0.5, color=ps.PRIMARY)
    ax.set_yticks(y)
    ax.set_yticklabels(wrapped)
    ax.tick_params(axis="y", length=0, labelsize=ps.font_size("small"))
    ax.bar_label(bars, labels=[ps.fmt_count(v) for v in values], padding=3, fontsize=ps.font_size("small"), color=ps.TEXT)
    ax.set_xlim(0, max(values) * 1.12 if values else 1)
    ax.set_ylim(-0.6, len(values) - 0.4)
    ps.style_axes(ax, integer_x=True)
    ax.spines["left"].set_visible(False)
    ax.xaxis.set_major_formatter(ps.COUNT_FORMATTER)
    ax.set_xlabel(xlabel)
    ps.set_title(ax, title, subtitle)
    return ps.save_figure(fig, path, message=message, dpi=dpi)


def plot_top_journals_bar(
    df,
    output_dir,
    topn=20,
    column="journal_name",
    plot_filename=None,
    counts_excel_filename="Journal_Counts_All.xlsx",
    dpi=None,
    figsize=None
):
    """
    Journal analysis:
      - Saves ALL journal counts to a single Excel file in output_dir
      - Plots only the top N journals

    Args:
        df (pd.DataFrame): Input DataFrame containing journal information.
        output_dir (str): Global analysis output directory.
        topn (int): Number of top journals to plot.
        column (str): Column containing journal names.
        plot_filename (str | None): Output plot filename. Auto-generated if None.
        counts_excel_filename (str): Excel filename storing full journal counts.
        dpi (int): Figure DPI.
        figsize (tuple): Figure size.
        show (bool): Display the plot (debugging).

    Returns:
        pd.Series: Full journal counts (descending).
    """

    if column not in df.columns:
        raise ValueError(
            f"[plot_top_journals_bar] Column '{column}' not found. "
            f"Available columns: {list(df.columns)}"
        )


    # ---- Compute ALL journal counts ----
    journal_counts_all = (
        df[column]
        .dropna()
        .astype(str)
        .value_counts()
    )
    # equal counts in name order, so the ranking does not depend on the pandas version
    journal_counts_all = journal_counts_all.sort_index().sort_values(ascending=False, kind="stable")

    if journal_counts_all.empty:
        raise ValueError("[plot_top_journals_bar] No journal data available.")

    # ---- Save counts to Excel (overwrite each run) ----
    counts_df = (
        journal_counts_all
        .rename_axis("journal_name")
        .reset_index(name="paper_count")
    )

    counts_path = os.path.join(output_dir, counts_excel_filename)
    counts_df.to_excel(counts_path, index=False)

    # ---- Plot TOP N only ----
    if plot_filename is None:
        plot_filename = f"Top_{topn}_Journals_Bar.png"

    plot_path = os.path.join(output_dir, plot_filename)

    top_desc = journal_counts_all.head(topn)
    n_journals = len(journal_counts_all)
    subtitle = (f"All {len(df):,} papers are from one journal" if n_journals == 1 else
                f"Top {len(top_desc)} of {n_journals:,} journals by number of papers | n = {len(df):,} papers")
    plot_ranked_barh(top_desc.index, top_desc.values, plot_path, "Journals with the most papers", subtitle,
                     message="Top journals bar plot", figsize=figsize, dpi=dpi)


# ------------------------------ Top Authors ----------------------------

UNKNOWN_INSTITUTIONS = {"U", "UNK", "UNKNOWN", "N/A", "NA", "NONE", "NULL"}


def plot_top_authors_bar(
    df,
    output_dir,
    topn=20,
    column="author_institution",
    first_author_only=False,
    include_institution_name=False,
    plot_filename=None,
    counts_excel_filename="Author_Counts_All.xlsx",
    dpi=None,
    figsize=None,
):

    """
    Top Authors bar plot with optional First-Author-only counting and optional institution labels.

    Modes:
      1) include_institution_name=False (default)
         - Uses 'column' (default: author_institution)
         - Counts authors only

      2) include_institution_name=True
         - Uses 'author_institution_llm' (requires Institutions Processing module)
         - If missing, prints instructions and returns None
         - Counts authors and stores the most frequent institution per author
         - Excel output includes institution column
         - Plot labels show "Author - Institution"

    Expected format per row:
      "(Author||Institution1|Institution2), (Author2||InstitutionX), ..."

    Returns:
        pd.DataFrame | None:
          - If include_institution_name=False: columns ["author","paper_count"]
          - If include_institution_name=True: columns ["author","institution","paper_count"]
          - None if skipped due to missing author_institution_llm
    """

    UNKNOWN_INSTITUTIONS = {"U", "UNK", "UNKNOWN", "N/A", "NA", "NONE", "NULL"}


    # ---- Decide which column to use ----
    if include_institution_name:
        llm_col = "author_institution_llm"
        if llm_col not in df.columns:
            msg = (
                f"[SKIP] Column '{llm_col}' not found.\n"
                "To include institution names in the Top Authors plot, you must run the Institutions Processing module first.\n\n"
                "Please:\n"
                "  1. Set OPENAI_API_KEY (or 'open_ai_api_key' in the config file)\n"
                "  2. Run the institutions module (argument: 'institution-processing')\n"
                "  3. Re-run the analysis step\n"
            )
            print(msg)
            return None
        use_col = llm_col
    else:
        use_col = column
        if use_col not in df.columns:
            raise ValueError(
                f"[plot_top_authors_bar] Column '{use_col}' not found. "
                f"Available columns: {list(df.columns)}"
            )

    author_counter = Counter()

    # When include_institution_name=True, track institution frequencies per author
    author_inst_counter = defaultdict(Counter)

    for entry in df[use_col].dropna().astype(str):

        # ---------- FIRST AUTHOR ONLY ----------
        if first_author_only:
            match = re.search(r"\(([^()]*)\)", entry)
            if not match:
                continue

            first_tuple = match.group(1)
            if "||" not in first_tuple:
                continue

            author = first_tuple.split("||", 1)[0].strip()
            if not author:
                continue

            author_counter[author] += 1

            if include_institution_name:
                inst_block = first_tuple.split("||", 1)[1].strip().strip("() ")
                # pick the first non-unknown institution from the block
                insts = [s.strip() for s in re.split(r"\||;", inst_block) if s.strip()]
                inst = next((x for x in insts if x.upper() not in UNKNOWN_INSTITUTIONS and len(x) > 2), "")
                if inst:
                    author_inst_counter[author][inst] += 1

        # ---------- ALL AUTHORS ----------
        else:
            for pair in entry.split(","):
                if "||" not in pair:
                    continue

                # author name
                author = pair.split("||", 1)[0].strip().strip("() ").strip()
                if not author:
                    continue

                author_counter[author] += 1

                if include_institution_name:
                    inst_block = pair.split("||", 1)[1].strip().strip("() ")
                    insts = [s.strip() for s in re.split(r"\||;", inst_block) if s.strip()]
                    inst = next((x for x in insts if x.upper() not in UNKNOWN_INSTITUTIONS and len(x) > 2), "")
                    if inst:
                        author_inst_counter[author][inst] += 1

    if not author_counter:
        raise ValueError("[plot_top_authors_bar] No authors parsed from the provided DataFrame.")

    # ---- Build counts df ----
    if include_institution_name:
        rows = []
        for author, count in author_counter.items():
            inst = ""
            if author_inst_counter.get(author):
                inst = author_inst_counter[author].most_common(1)[0][0]
            rows.append((author, inst, count))

        counts_df = pd.DataFrame(rows, columns=["author", "institution", "paper_count"])
        counts_df = counts_df.sort_values(["paper_count", "author"], ascending=[False, True], kind="stable").reset_index(drop=True)
    else:
        counts_df = (
            pd.DataFrame(author_counter.items(), columns=["author", "paper_count"])
            .sort_values(["paper_count", "author"], ascending=[False, True], kind="stable")
            .reset_index(drop=True)
        )

    # ---- Suffix for filenames ----
    suffix_parts = []
    suffix_parts.append("FirstAuthor" if first_author_only else "AllAuthors")
    if include_institution_name:
        suffix_parts.append("WithInstitution")
    suffix = "_".join(suffix_parts)

    # ---- Save ALL counts to Excel ----
    os.makedirs(output_dir, exist_ok=True)

    # If user left default filename, auto-suffix it
    if counts_excel_filename == "Author_Counts_All.xlsx":
        counts_excel_filename = f"Author_Counts_{suffix}.xlsx"

    counts_path = os.path.join(output_dir, counts_excel_filename)
    counts_df.to_excel(counts_path, index=False)

    # ---- Plot TOP N only ----
    top_df = counts_df.head(topn).copy()
    top_df = top_df.iloc[::-1]  # ascending for barh

    if plot_filename is None:
        plot_filename = f"Top_{topn}_Authors_{suffix}.png"

    plot_path = os.path.join(output_dir, plot_filename)

    # y-axis labels
    if include_institution_name:
        # readable label; change to "\n" if you prefer multi-line
        top_df["label"] = top_df["author"] + " - " + top_df["institution"].fillna("").astype(str)
        y_labels = top_df["label"]
    else:
        y_labels = top_df["author"]

    rule = ["by number of papers"]
    if first_author_only:
        rule.append("first author only")
    if include_institution_name:
        rule.append("most frequent institution shown")
    top_desc = top_df.iloc[::-1]
    plot_ranked_barh(list(y_labels)[::-1], top_desc["paper_count"], plot_path, "Most productive authors",
                     f"Top {len(top_desc)} {', '.join(rule)} | n = {len(df):,} papers",
                     message="Top authors bar plot", figsize=figsize, dpi=dpi)

    return counts_df



# ---------------------------- Top Insitutions ---------------------------



def plot_top_first_author_institutions_bar(
    df,
    output_dir,
    topn=20,
    column="author_institution_llm",
    plot_filename=None,
    counts_excel_filename="Institution_Counts_All.xlsx",
    dpi=None,
    figsize=None,
):
    """
    Top First-Author Institutions (LLM-normalized):

    - Uses `author_institution_llm` (generated via Institutions Processing module)
    - Filters unknown / placeholder institutions (e.g., 'U', 'UNKNOWN')
    - Saves ALL institution counts to Excel
    - Plots only Top N institutions

    Expected format per row:
      "(Author||Institution1|Institution2), (Author2||InstitutionX), ..."

    Returns:
        pd.DataFrame | None:
            Full institution counts with columns
            ["institution", "paper_count"], or None if skipped.
    """


    UNKNOWN_INSTITUTIONS = {
                      "U", "UNKNOWN", "N/A", "NA", "NONE", "NULL"
                        }


    # ---- Column existence check ----
    if column not in df.columns:
        msg = (
            f"[SKIP] Column '{column}' not found.\n"
            "Top Institutions plot requires running the Institutions Processing module.\n\n"
            "Please:\n"
            "  1. Set OPENAI_API_KEY (or 'open_ai_api_key' in the config file)\n"
            "  2. Run the institutions module (argument: 'institution-processing')\n"
            "  3. Re-run the analysis step\n"
        )
        print(msg)
        return None

    inst_counter = Counter()

    for entry in df[column].dropna().astype(str):
        match = re.search(r"\(([^()]*)\)", entry)
        if not match:
            continue

        first_tuple = match.group(1)
        if "||" not in first_tuple:
            continue

        _, inst_block = first_tuple.split("||", 1)

        # Institutions may be separated by | or ;
        for inst in re.split(r"\||;", inst_block):
            inst = inst.strip()

            if not inst:
                continue

            if inst.upper() in UNKNOWN_INSTITUTIONS:
                continue

            # Optional but very safe
            if len(inst) <= 2:
                continue

            inst_counter[inst] += 1

    if not inst_counter:
        print("[WARN] No valid institutions could be parsed from the data.")
        return None

    # ---- Full counts dataframe ----
    counts_df = (
        pd.DataFrame(inst_counter.items(), columns=["institution", "paper_count"])
        .sort_values(["paper_count", "institution"], ascending=[False, True], kind="stable")
        .reset_index(drop=True)
    )

    # ---- Save ALL counts to Excel ----
    os.makedirs(output_dir, exist_ok=True)
    counts_path = os.path.join(output_dir, counts_excel_filename)
    counts_df.to_excel(counts_path, index=False)

    # ---- Plot TOP N only ----
    top_df = counts_df.head(topn).iloc[::-1]  # ascending for barh

    if plot_filename is None:
        plot_filename = f"Top_{topn}_FirstAuthor_Institutions_Bar.png"

    plot_path = os.path.join(output_dir, plot_filename)

    top_desc = top_df.iloc[::-1]
    plot_ranked_barh(top_desc["institution"], top_desc["paper_count"], plot_path, "Institutions of first authors",
                     f"Top {len(top_desc)} institutions by number of papers (first author's institution) | n = {len(df):,} papers",
                     message="Top institutions bar plot", figsize=figsize, dpi=dpi)

    return counts_df


# ---------------- Common functions for author & institutions graph network -----------------

def edges_df_to_gephi_csv(
    edges_df: pd.DataFrame,
    output_nodes_csv: str,
    output_edges_csv: str,
    *,
    source_col: str = "Source",
    target_col: str = "Target",
    weight_col: str = "Weight",
    source_comm_col: str = "Source_Community",
    target_comm_col: str = "Target_Community",
):
    """
    Convert an edge list DataFrame with community columns into Gephi-ready nodes/edges CSVs.

    Required columns by default:
      Source, Target, Weight, Source_Community, Target_Community
    """
    required = {source_col, target_col, weight_col, source_comm_col, target_comm_col}
    missing = required - set(edges_df.columns)
    if missing:
        raise ValueError(f"[edges_df_to_gephi_csv] Missing columns: {sorted(missing)}")

    sources = edges_df[[source_col, source_comm_col]].rename(
        columns={source_col: "Id", source_comm_col: "Community"}
    )
    targets = edges_df[[target_col, target_comm_col]].rename(
        columns={target_col: "Id", target_comm_col: "Community"}
    )

    nodes_df = (
        pd.concat([sources, targets], ignore_index=True)
        .drop_duplicates(subset=["Id"])
        .copy()
    )
    nodes_df["Label"] = nodes_df["Id"]
    nodes_df = nodes_df[["Id", "Label", "Community"]]

    edges_gephi_df = edges_df[[source_col, target_col, weight_col]].rename(
        columns={source_col: "Source", target_col: "Target", weight_col: "Weight"}
    )

    os.makedirs(os.path.dirname(output_nodes_csv) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(output_edges_csv) or ".", exist_ok=True)

    nodes_df.to_csv(output_nodes_csv, index=False)
    edges_gephi_df.to_csv(output_edges_csv, index=False)

    print(f"[INFO] Gephi nodes saved to: {output_nodes_csv}")
    print(f"[INFO] Gephi edges saved to: {output_edges_csv}")


def _sort_pairs(pair_df):
    """Pair table sorted by Count (descending), ties by the pair names, so row order is reproducible."""
    names = [c for c in pair_df.columns if c != "Count"]
    return pair_df.sort_values(["Count"] + names, ascending=[False] + [True] * len(names), kind="mergesort")


def _canonical_graph(G):
    """
    Copy of G with nodes and edges in sorted order. Layout, community detection and tie-breaking then
    depend only on the data, not on Python's per-run string-hash order, so repeated runs give the same result.
    """
    H = nx.Graph()
    H.add_nodes_from(sorted(G.nodes()))
    H.add_edges_from(sorted(((min(u, v), max(u, v), d) for u, v, d in G.edges(data=True)), key=lambda e: (e[0], e[1])))
    return H


def _top_nodes_by_degree(G, n):
    """The n nodes with the most links; ties broken by name (deterministic)."""
    degree = dict(G.degree())
    return sorted(degree, key=lambda node: (-degree[node], node))[:n]


def summarize_communities_generic(Gf, communities, output_dir, entity_label="Author", filename_prefix="AuthorGraph"):
    summary = []
    for i, community_nodes in enumerate(communities):
        subgraph = Gf.subgraph(community_nodes)
        degrees = dict(subgraph.degree())
        top_nodes = sorted(degrees.items(), key=lambda x: (-x[1], x[0]))[:5]
        summary.append({
            "Community ID": i,
            "Node Count": len(community_nodes),
            f"Top {entity_label}s (by degree)": ", ".join([n for n, _ in top_nodes]),
            f"All {entity_label}s": ", ".join(sorted(community_nodes))
        })

    summary_df = pd.DataFrame(summary)
    os.makedirs(output_dir, exist_ok=True)
    summary_path = os.path.join(output_dir, f"{filename_prefix}_Communities_Summary.xlsx")
    summary_df.to_excel(summary_path, index=False)
    print(f"[INFO] Community summary saved to: {summary_path}")



def export_edge_data_generic(
    Gf,
    color_map,
    output_dir,
    filename_prefix="Graph",
    export_gephi=True,
):
    """
    Exports an edge list with community IDs to Excel, and optionally
    writes Gephi-ready nodes/edges CSV files.

    Outputs:
      {prefix}_Edges_With_Communities.xlsx
      {prefix}_gephi_nodes.csv
      {prefix}_gephi_edges.csv
      Works for Authors / Institutions / MeSH (anything using same schema)
    """
    edge_data = []
    for u, v, d in Gf.edges(data=True):
        edge_data.append({
            "Source": u,
            "Target": v,
            "Weight": d.get("weight", 1),
            "Source_Community": color_map.get(u, -1),
            "Target_Community": color_map.get(v, -1),
        })

    edge_df = pd.DataFrame(edge_data)
    os.makedirs(output_dir, exist_ok=True)

    # Excel edge list
    edge_xlsx = os.path.join(output_dir, f"{filename_prefix}_Edges_With_Communities.xlsx")
    edge_df.to_excel(edge_xlsx, index=False)
    print(f"[INFO] Edge list with community info saved to: {edge_xlsx}")

    # Gephi CSVs (optional)
    if export_gephi:
        nodes_csv = os.path.join(output_dir, f"{filename_prefix}_gephi_nodes.csv")
        edges_csv = os.path.join(output_dir, f"{filename_prefix}_gephi_edges.csv")
        edges_df_to_gephi_csv(edge_df, nodes_csv, edges_csv)

    return edge_df


# ------------------------------- MeSH graph network ------------------------------

def _place_labels_without_overlap(ax, texts, anchors, max_rounds=3):
    """
    Places node labels with adjust_text; if labels still overlap afterwards (very dense layouts),
    the label of the less-connected node (texts are ordered best-connected first) is removed and
    placement is redone, so the saved figure never has colliding labels.
    """
    if not texts:
        return texts
    fig = ax.figure
    for _ in range(max_rounds):
        before = set(ax.get_children())
        _rng_state = np.random.get_state()
        np.random.seed(0)   # reproducible label placement
        # fixed number of iterations (no time limit), so the result does not depend on machine speed
        adjust_text(texts, ax=ax, expand=(1.2, 1.4), arrowprops=dict(arrowstyle="-", color=ps.GREY_DARK, lw=0.5),
                    time_lim=None, iter_lim=250)
        np.random.set_state(_rng_state)
        added = [a for a in ax.get_children() if a not in before]
        fig.canvas.draw()
        r = fig.canvas.get_renderer()
        boxes = [t.get_window_extent(r) for t in texts]
        drop = set()
        for i in range(len(texts)):
            if i in drop:
                continue
            for j in range(i + 1, len(texts)):
                if j not in drop and boxes[i].overlaps(boxes[j]):
                    drop.add(j)
        if not drop:
            return texts
        for a in added:          # remove leader lines, reset and retry with the remaining labels
            a.remove()
        keep = []
        for k, (t, xy) in enumerate(zip(texts, anchors, strict=False)):
            if k in drop:
                t.remove()
            else:
                t.set_position(xy)
                keep.append((t, xy))
        texts, anchors = [t for t, _ in keep], [xy for _, xy in keep]
    return texts


def draw_network(Gf, pos, communities_, edge_thickness, path, title, subtitle, label_top_n=30, entity_label="node",
                 figsize=(12, 9), dpi=None, message="Graph network"):
    """
    Shared drawing for the MeSH, author and institution networks.

    - colours: communities ranked by size; the 7 largest get distinct colours, the rest share grey
      (community IDs in the exported Excel/Gephi files are unchanged)
    - node size by number of links; light grey links; labels only for the label_top_n nodes with
      the most links (0 = none, -1 = all), placed without overlaps
    - legend outside the plot: each coloured community with its best-connected members
    """
    import matplotlib.patheffects as pe
    from matplotlib.lines import Line2D

    order = sorted(range(len(communities_)), key=lambda i: len(communities_[i]), reverse=True)
    palette = ps.community_colors(len(order))
    comm_color = {cid: palette[rank] for rank, cid in enumerate(order)}
    node_comm = {n: i for i, c in enumerate(communities_) for n in c}
    nodes = list(Gf.nodes())
    degree = dict(Gf.degree())
    max_deg = max(degree.values()) if degree else 1
    sizes = [30 + 570 * (degree[n] / max_deg) ** 0.75 for n in nodes]

    fig, ax = plt.subplots(figsize=figsize)
    nx.draw_networkx_edges(Gf, pos, ax=ax, width=[max(0.3, w) for w in edge_thickness], edge_color=ps.GREY, alpha=0.35)
    nx.draw_networkx_nodes(Gf, pos, ax=ax, nodelist=nodes, node_size=sizes,
                           node_color=[comm_color[node_comm[n]] for n in nodes], edgecolors="white", linewidths=0.6)

    ranked = sorted(nodes, key=lambda n: (-degree[n], n))
    to_label = ranked if label_top_n == -1 else ranked[:max(0, label_top_n)]
    texts = []
    for n in to_label:
        x, y = pos[n]
        t = ax.text(x, y, ps.shorten(n, 30), fontsize=ps.font_size("small") * (1.0 if len(to_label) <= 60 else 0.85),
                    ha="center", va="center", color=ps.TEXT, zorder=5)
        t.set_path_effects([pe.withStroke(linewidth=2.5, foreground="white")])
        texts.append(t)
    ax.set_axis_off()
    ax.margins(0.04)
    texts = _place_labels_without_overlap(ax, texts, [pos[n] for n in to_label])
    if subtitle and to_label and len(to_label) == len(nodes) and len(texts) == len(to_label):
        subtitle = re.sub(r"labels: (\d+) best-connected( terms)?", lambda m: f"labels: all {m.group(1)}{m.group(2) or ''}", subtitle)
    if subtitle and len(texts) < len(to_label):   # some labels were hidden to avoid collisions
        subtitle = re.sub(r"labels: (\d+) best-connected( terms)?",
                          lambda m: f"labels: {len(texts)} of the {m.group(1)} best-connected{m.group(2) or ''} (rest hidden to avoid overlap)", subtitle)

    handles = []
    for rank, cid in enumerate(order):
        if palette[rank] == ps.OTHER:
            rest = len(order) - rank
            handles.append(Line2D([0], [0], marker="o", ls="", markerfacecolor=ps.OTHER, markeredgecolor="white", markersize=9,
                                  label=f"{rest} smaller {'community' if rest == 1 else 'communities'}"))
            break
        members = sorted(communities_[cid], key=lambda n: (-degree[n], n))
        name_len = 30 if entity_label == "institution" else 24
        head = f"Community {cid} ({len(members)}): "
        names_ = [ps.shorten(m, name_len) for m in members[:3]]
        while len(names_) > 1 and len(head + "; ".join(names_)) > 75:   # drop whole names rather than cut mid-name
            names_.pop()
        entry = head + "; ".join(names_)
        handles.append(Line2D([0], [0], marker="o", ls="", markerfacecolor=palette[rank], markeredgecolor="white", markersize=9,
                              label=entry))
    ps.legend_below(ax, handles, title=f"Communities (members; best-connected {entity_label}s)", max_cols=3)
    ps.set_title(ax, title, subtitle)
    return ps.save_figure(fig, path, message=message, dpi=dpi)


#1
def extract_mesh_pairs_from_matrix(df_matrix: pd.DataFrame) -> pd.DataFrame:
    """
    Convert a MeSH co-occurrence matrix into a pair list DataFrame.

    Expects:
      - index = terms
      - columns = terms
      - symmetric matrix

    Returns:
        DataFrame with columns ["Term 1", "Term 2", "Count"]
    """
    pairs = []

    terms = df_matrix.index.tolist()
    for i, t1 in enumerate(terms):
        for j in range(i + 1, len(terms)):
            t2 = terms[j]
            w = df_matrix.iloc[i, j]
            if w > 0:
                pairs.append((t1, t2, int(w)))

    return pd.DataFrame(pairs, columns=["Term 1", "Term 2", "Count"])

#2
def generate_mesh_graph_network_plot(
    cooccur_file,
    output_dir,
    top_n=200,
    threshold_percentile=95,
    edge_thickness_multiplier=5,
    min_degree=2,
    show_labels=True,
    filename_prefix="MeSHGraph",
    label_top_n=30,
    n_papers=None,
):
    """
    Generates a MeSH term network from a co-occurrence matrix Excel file (Sheet1),
    exports a FULL pair list (like authors/institutions), applies percentile thresholding,
    runs community detection, saves plot + summaries, and exports Gephi-ready CSVs
    via export_edge_data_generic().

    Notes:
      - `top_n` is primarily used for filename parity unless the matrix itself is top-n limited.
      - Requires common utilities:
          summarize_communities_generic(...)
          export_edge_data_generic(...)
    """

    # ----------------------------
    # Load co-occurrence matrix
    # ----------------------------
    df_matrix = pd.read_excel(cooccur_file, sheet_name="Sheet1")

    if "Unnamed: 0" not in df_matrix.columns:
        raise ValueError(
            "[generate_mesh_graph_network_plot] Expected column 'Unnamed: 0' in Sheet1 "
            "(matrix index column)."
        )

    df_matrix = df_matrix.set_index("Unnamed: 0")

    # Ensure numeric
    df_matrix = df_matrix.apply(pd.to_numeric, errors="coerce").fillna(0)

    # ----------------------------
    # Export FULL pair list (upper triangle, >0)
    # ----------------------------
    terms = df_matrix.index.tolist()
    pairs = []
    for i, t1 in enumerate(terms):
        row_vals = df_matrix.iloc[i].values
        for j in range(i + 1, len(terms)):
            w = row_vals[j]
            if w > 0:
                pairs.append((t1, terms[j], int(w)))

    pair_df = pd.DataFrame(pairs, columns=["Term 1", "Term 2", "Count"])

    if pair_df.empty:
        print("[WARNING] No MeSH pairs found in co-occurrence matrix.")
        return None

    os.makedirs(output_dir, exist_ok=True)
    pair_path = os.path.join(output_dir, "MeSH_Pairs.xlsx")
    _sort_pairs(pair_df).to_excel(pair_path, index=False)
    print(f"[INFO] MeSH pair list saved to: {pair_path}")

    # ----------------------------
    # Threshold and edge filtering
    # ----------------------------
    threshold_value = np.percentile(pair_df["Count"], threshold_percentile)
    key_edges = pair_df[pair_df["Count"] >= threshold_value]

    if key_edges.empty:
        print("[WARNING] No edges left after percentile thresholding.")
        return None

    # ----------------------------
    # Build and filter graph
    # ----------------------------
    G = nx.Graph()
    for _, row in key_edges.iterrows():
        G.add_edge(row["Term 1"], row["Term 2"], weight=row["Count"])
    G = _canonical_graph(G)

    filtered_nodes = [node for node in G.nodes() if G.degree(node) > min_degree]
    Gf = G.subgraph(filtered_nodes).copy()

    if len(Gf.nodes) == 0:
        print("[WARNING] No nodes left after filtering. Adjust threshold or min_degree.")
        return None

    # ----------------------------
    # Visual attributes
    # ----------------------------
    edge_weights = [Gf[u][v]["weight"] for u, v in Gf.edges()]
    max_weight = max(edge_weights) if edge_weights else 1
    edge_thickness = [(w / max_weight) * edge_thickness_multiplier for w in edge_weights]

    # ----------------------------
    # Communities + modularity
    # ----------------------------
    communities_ = list(community.greedy_modularity_communities(Gf))
    color_map = {node: i for i, c in enumerate(communities_) for node in c}

    modularity = community.modularity(Gf, communities_)
    print(f"[INFO] Modularity score: {modularity:.4f}")

    # ----------------------------
    # Layout
    # ----------------------------
    pos = nx.kamada_kawai_layout(Gf, scale=3)

    # ----------------------------
    # Plot (same style as author/institution graphs)
    # ----------------------------
    filename = f"{filename_prefix}_T{top_n}_P{threshold_percentile}_D{min_degree}_W{int(edge_thickness_multiplier)}.png"
    graph_path = os.path.join(output_dir, filename)
    shown = 0 if not show_labels else (label_top_n if label_top_n != -1 else len(Gf))
    draw_network(Gf, pos, communities_, edge_thickness, graph_path, "MeSH terms that appear together",
                 f"{len(Gf):,} terms, {Gf.number_of_edges():,} links | link = papers sharing both terms, kept if \u2265 {threshold_value:.0f} papers "
                 f"({threshold_percentile:g}th percentile) | node size = number of links | labels: {min(shown, len(Gf))} best-connected terms"
                 + (f" | n = {n_papers:,} papers" if n_papers else ""),
                 label_top_n=label_top_n if show_labels else 0, entity_label="term", figsize=(14, 10), message="Graph network")

    # ----------------------------
    # Save summaries + Gephi files (common utilities)
    # ----------------------------
    summarize_communities_generic(
        Gf,
        communities_,
        output_dir,
        entity_label="MeSH_Term",
        filename_prefix=filename_prefix,
    )

    export_edge_data_generic(
        Gf,
        color_map,
        output_dir,
        filename_prefix=filename_prefix,
        export_gephi=True,
    )

    return Gf


# ---------------------------- Author collaboration graph network ---------------------------

# 1
def extract_author_pairs_from_df(
    df: pd.DataFrame,
    author_col: str = "author_institution",
    ) -> pd.DataFrame:
    """
    Extract co-author pairs and their collaboration counts from a pipeline DataFrame.

    Expects df[author_col] entries like:
      "(Author1||Inst1), (Author2||Inst2), ..."

    Returns:
        pd.DataFrame with columns: ["Author 1", "Author 2", "Count"]
    """
    if author_col not in df.columns:
        raise ValueError(
            f"[extract_author_pairs_from_df] Column '{author_col}' not found. "
            f"Available columns: {list(df.columns)}"
        )

    counter = Counter()

    for entry in df[author_col].dropna().astype(str):
        try:
            pairs = entry.split(",")
            authors = []
            for pair in pairs:
                if "||" in pair:
                    name = pair.split("||", 1)[0].strip("() ").strip()
                    if name:
                        authors.append(name)

            # unique co-authors per paper/row
            for a1, a2 in combinations(sorted(set(authors)), 2):
                counter[(a1, a2)] += 1
        except Exception:
            continue

    pair_df = pd.DataFrame(
        [(a1, a2, count) for (a1, a2), count in counter.items()],
        columns=["Author 1", "Author 2", "Count"]
    )

    return pair_df


# 2
def generate_collab_graph_from_pair_df(
    pair_df: pd.DataFrame,
    output_dir: str,
    *,
    entity_label: str = "Author",
    filename_prefix: str = "AuthorGraph",
    top_n: int = 200,
    threshold_percentile: float = 99,
    min_edge_count: int = 3,
    edge_thickness_multiplier: float = 5,
    min_degree: int = 2,
    show_labels: bool = True,
    include_neighbors: bool = False,
    dpi: int = None,
    figsize=(12, 9),
    label_top_n: int = 30,
    n_papers: int = None,
    ):
    """
    Build + save a collaboration network graph from a pair edge DataFrame.

    pair_df must have: ["Author 1", "Author 2", "Count"] (or analogous names).
    This function assumes those exact column names for now to match your pipeline.
    """
    required = {"Author 1", "Author 2", "Count"}
    if not required.issubset(pair_df.columns):
        raise ValueError(
            f"[generate_collab_graph_from_pair_df] pair_df must contain {sorted(required)}. "
            f"Got: {list(pair_df.columns)}"
        )

    df = pair_df.copy()

    # --- ADAPTIVE EDGE FILTERING ---
    if len(df) >= 5000:
        threshold_value = np.percentile(df["Count"], threshold_percentile)
        print(f"[INFO] Using percentile-based threshold: Count = {threshold_value:.2f}")
        df = df[df["Count"] >= threshold_value]
        rule = f"kept if \u2265 {threshold_value:.0f} joint paper{'' if round(threshold_value) == 1 else 's'} ({threshold_percentile:g}th percentile)"
    else:
        print(f"[INFO] Using fixed threshold: Count = {min_edge_count}")
        df = df[df["Count"] >= min_edge_count]
        rule = f"kept if \u2265 {min_edge_count} joint paper{'' if min_edge_count == 1 else 's'}"

    if df.empty:
        print("[WARNING] No edges left after filtering.")
        return None

    # --- BUILD FULL GRAPH BEFORE top_n ---
    G = nx.Graph()
    for _, row in df.iterrows():
        G.add_edge(row["Author 1"], row["Author 2"], weight=row["Count"])
    G = _canonical_graph(G)

    # --- SELECT TOP N NODES BY DEGREE ---
    top_nodes = _top_nodes_by_degree(G, top_n)

    # --- Optionally include 1-hop neighbors ---
    if include_neighbors:
        neighbors = set()
        for node in top_nodes:
            neighbors.update(G.neighbors(node))
        top_nodes = set(top_nodes).union(neighbors)

    # --- Subgraph with top nodes ---
    G = G.subgraph(top_nodes).copy()

    # --- Apply degree filter ---
    Gf = G.subgraph([n for n in G.nodes if G.degree(n) >= min_degree]).copy()
    if len(Gf.nodes) == 0:
        print("[WARNING] No nodes left after min_degree filter.")
        return None

    # --- Layout & visual attributes ---
    pos = nx.kamada_kawai_layout(Gf, scale=3)
    edge_weights = [Gf[u][v]["weight"] for u, v in Gf.edges()]
    max_weight = max(edge_weights) if edge_weights else 1
    edge_thickness = [(w / max_weight) * edge_thickness_multiplier for w in edge_weights]

    # --- Community Detection ---
    communities = list(community.greedy_modularity_communities(Gf))
    color_map = {node: i for i, comm in enumerate(communities) for node in comm}
    modularity = community.modularity(Gf, communities)
    print(f"[INFO] Modularity score: {modularity:.4f}")

    # --- Plot ---
    os.makedirs(output_dir, exist_ok=True)
    filename = f"{filename_prefix}_T{top_n}_P{threshold_percentile}_D{min_degree}_W{edge_thickness_multiplier}.png"
    graph_path = os.path.join(output_dir, filename)
    shown = 0 if not show_labels else (label_top_n if label_top_n != -1 else len(Gf))
    draw_network(Gf, pos, communities, edge_thickness, graph_path, f"{entity_label} collaboration network",
                 f"{len(Gf):,} {entity_label.lower()}s, {Gf.number_of_edges():,} links | link = co-authored papers, {rule} | "
                 f"node size = number of collaborators | labels: {min(shown, len(Gf))} best-connected"
                 + (f" | n = {n_papers:,} papers" if n_papers else ""),
                 label_top_n=label_top_n if show_labels else 0, entity_label=entity_label.lower(),
                 figsize=figsize, dpi=dpi, message=f"{entity_label} Collaboration Network Graph")

    # --- Save community + edge summaries ---
    summarize_communities_generic(Gf, communities, output_dir, entity_label=entity_label, filename_prefix=filename_prefix)
    export_edge_data_generic(Gf, color_map, output_dir, filename_prefix=filename_prefix)

    return Gf


# 3 main
def generate_author_collab_graph_from_pipeline_df(
    df: pd.DataFrame,
    output_dir: str,
    *,
    author_col: str = "author_institution",
    top_n: int = 200,
    threshold_percentile: float = 99,
    min_edge_count: int = 3,
    edge_thickness_multiplier: float = 5,
    min_degree: int = 2,
    show_labels: bool = True,
    label_top_n: int = 30,
    include_neighbors: bool = True#False,
    ):
    pair_df = extract_author_pairs_from_df(df, author_col=author_col)

    if pair_df.empty:
        print("[WARNING] No author pairs extracted; skipping author network graph.")
        return None

    # Optional: save the raw pair list too (often handy)
    pair_path = os.path.join(output_dir, "Author_Collaboration_Pairs.xlsx")
    os.makedirs(output_dir, exist_ok=True)
    _sort_pairs(pair_df).to_excel(pair_path, index=False)

    return generate_collab_graph_from_pair_df(
        pair_df,
        output_dir,
        entity_label="Author",
        filename_prefix="AuthorGraph",
        top_n=top_n,
        threshold_percentile=threshold_percentile,
        min_edge_count=min_edge_count,
        edge_thickness_multiplier=edge_thickness_multiplier,
        min_degree=min_degree,
        show_labels=show_labels,
        include_neighbors=include_neighbors,
        label_top_n=label_top_n,
        n_papers=len(df),
    )


# ------------------------- Institutions collaboration graph network ------------------------

#1
def extract_institution_pairs_from_df(
    df: pd.DataFrame,
    author_col: str = "author_institution_llm",
    top_n: int = 200,
    drop_unknown: bool = True,
) -> pd.DataFrame | None:
    """
    Build institution co-occurrence pairs from a pipeline df.

    - First pass: count institution frequency across all tuples.
    - Select top_n institutions.
    - Second pass: build pair counts among only top_n institutions.

    Returns:
        pd.DataFrame with columns ["Institution 1", "Institution 2", "Count"]
        or None if author_col missing / no data.
    """
    if author_col not in df.columns:
        msg = (
            f"[SKIP] Column '{author_col}' not found.\n"
            "Institution network requires the Institutions module.\n\n"
            "  Please:\n"
            "  1. Set OPENAI_API_KEY (or 'open_ai_api_key' in the config file)\n"
            "  2. Run the institutions module first (to generate author_institution_llm)\n"
            "  3. Re-run the analysis step\n"
        )
        print(msg)
        return None

    unknown_set = {"UNKNOWN", "U", "UNK"} if drop_unknown else set()

    # ---------- Pass 1: institution frequency ----------
    inst_freq = Counter()
    for entry in df[author_col].dropna().astype(str):
        for t in re.findall(r"\(([^()]*)\)", entry):
            if "||" in t:
                inst = t.split("||", 1)[1].strip().rstrip(" ,.;")
                if not inst:
                    continue
                if drop_unknown and inst.upper() in unknown_set:
                    continue
                inst_freq[inst] += 1

    if not inst_freq:
        print("[WARN] No institutions parsed from author_institution_llm.")
        return None

    top_insts = {inst for inst, _ in inst_freq.most_common(top_n)}
    print(f"[INFO] Selected top {len(top_insts)} institutions (top_n={top_n}).")

    # ---------- Pass 2: co-occurrence among top_n ----------
    pair_counter = Counter()
    for entry in df[author_col].dropna().astype(str):
        insts = set()
        for t in re.findall(r"\(([^()]*)\)", entry):
            if "||" in t:
                inst = t.split("||", 1)[1].strip().rstrip(" ,.;")
                if inst in top_insts:
                    insts.add(inst)

        for a, b in combinations(sorted(insts), 2):
            pair_counter[(a, b)] += 1

    if not pair_counter:
        print("[WARN] No institution co-occurrence pairs found after filtering to top_n.")
        return None

    pair_df = _sort_pairs(pd.DataFrame(
        [(a, b, c) for (a, b), c in pair_counter.items()],
        columns=["Institution 1", "Institution 2", "Count"]
    )).reset_index(drop=True)

    return pair_df


#2
def generate_institution_collab_graph_from_pair_df(
    pair_df: pd.DataFrame,
    output_dir: str,
    *,
    top_n: int = 200,
    threshold_percentile: float = 95,
    min_edge_count: int = 3,
    edge_thickness_multiplier: float = 5,
    min_degree: int = 2,
    show_labels: bool = True,
    include_neighbors: bool = False,
    dpi: int = None,
    figsize=(12, 9),
    label_top_n: int = 30,
    n_papers: int = None,
):
    required = {"Institution 1", "Institution 2", "Count"}
    if not required.issubset(pair_df.columns):
        raise ValueError(
            f"[generate_institution_collab_graph_from_pair_df] pair_df must contain {sorted(required)}. "
            f"Got: {list(pair_df.columns)}"
        )

    df = pair_df.copy()

    # --- adaptive-ish filtering (mirrors your author logic) ---
    if len(df) >= 300:
        threshold_value = np.percentile(df["Count"], threshold_percentile)
        print(f"[INFO] Using percentile-based threshold: Count = {threshold_value:.2f}")
        df = df[df["Count"] >= threshold_value]
        rule = f"kept if \u2265 {threshold_value:.0f} joint paper{'' if round(threshold_value) == 1 else 's'} ({threshold_percentile:g}th percentile)"
    else:
        print(f"[INFO] Using fixed threshold: Count = {min_edge_count}")
        df = df[df["Count"] >= min_edge_count]
        rule = f"kept if \u2265 {min_edge_count} joint paper{'' if min_edge_count == 1 else 's'}"

    if df.empty:
        print("[WARNING] No edges left after filtering.")
        return None

    # --- Build graph ---
    G = nx.Graph()
    for _, row in df.iterrows():
        G.add_edge(row["Institution 1"], row["Institution 2"], weight=row["Count"])
    G = _canonical_graph(G)

    # --- Select top_n nodes by degree (this is optional but keeps graphs stable/fast) ---
    top_nodes = _top_nodes_by_degree(G, top_n)

    if include_neighbors:
        neighbors = set()
        for node in top_nodes:
            neighbors.update(G.neighbors(node))
        top_nodes = set(top_nodes).union(neighbors)

    G = G.subgraph(top_nodes).copy()

    # --- Apply degree filter ---
    Gf = G.subgraph([n for n in G.nodes if G.degree(n) >= min_degree]).copy()
    if len(Gf.nodes) == 0:
        print("[WARNING] No nodes left after min_degree filter.")
        return None

    # --- Layout & visuals ---
    pos = nx.kamada_kawai_layout(Gf, scale=3)
    edge_weights = [Gf[u][v]["weight"] for u, v in Gf.edges()]
    max_weight = max(edge_weights) if edge_weights else 1
    edge_thickness = [(w / max_weight) * edge_thickness_multiplier for w in edge_weights]

    # --- Communities ---
    communities_ = list(community.greedy_modularity_communities(Gf))
    color_map = {node: i for i, comm in enumerate(communities_) for node in comm}
    modularity = community.modularity(Gf, communities_)
    print(f"[INFO] Modularity score: {modularity:.4f}")

    # --- Plot ---
    os.makedirs(output_dir, exist_ok=True)
    filename = f"InstitutionGraph_T{top_n}_P{threshold_percentile}_D{min_degree}_W{edge_thickness_multiplier}.png"
    graph_path = os.path.join(output_dir, filename)
    shown = 0 if not show_labels else (label_top_n if label_top_n != -1 else len(Gf))
    draw_network(Gf, pos, communities_, edge_thickness, graph_path, "Institution collaboration network",
                 f"{len(Gf):,} institutions, {Gf.number_of_edges():,} links | link = papers with authors from both, {rule} | "
                 f"node size = number of partner institutions | labels: {min(shown, len(Gf))} best-connected"
                 + (f" | n = {n_papers:,} papers" if n_papers else ""),
                 label_top_n=label_top_n if show_labels else 0, entity_label="institution",
                 figsize=figsize, dpi=dpi, message="Institution Collaboration Network Graph")

    # --- Save summaries using your common utilities ---
    summarize_communities_generic(
        Gf, communities_, output_dir,
        entity_label="Institution",
        filename_prefix="InstitutionGraph"
    )
    export_edge_data_generic(
        Gf, color_map, output_dir,
        filename_prefix="InstitutionGraph"
    )

    return Gf


#3
def generate_institution_collab_graph_from_pipeline_df(
    df: pd.DataFrame,
    output_dir: str,
    *,
    author_col: str = "author_institution_llm",
    top_n: int = 200,
    threshold_percentile: float = 95,
    min_edge_count: int = 3,
    edge_thickness_multiplier: float = 5,
    min_degree: int = 2,
    show_labels: bool = True,
    label_top_n: int = 30,
    include_neighbors: bool = True#False,
):
    pair_df = extract_institution_pairs_from_df(
        df,
        author_col=author_col,
        top_n=top_n,
        drop_unknown=True
    )

    if pair_df is None or pair_df.empty:
        print("[WARNING] No institution pairs extracted; skipping institution network graph.")
        return None

    os.makedirs(output_dir, exist_ok=True)
    pair_path = os.path.join(output_dir, "Institution_Collaboration_Pairs.xlsx")
    pair_df.to_excel(pair_path, index=False)
    print(f"[INFO] Institution pair list saved to: {pair_path}")

    return generate_institution_collab_graph_from_pair_df(
        pair_df,
        output_dir,
        top_n=top_n,
        threshold_percentile=threshold_percentile,
        min_edge_count=min_edge_count,
        edge_thickness_multiplier=edge_thickness_multiplier,
        min_degree=min_degree,
        show_labels=show_labels,
        include_neighbors=include_neighbors,
        label_top_n=label_top_n,
        n_papers=len(df),
    )


# --------------------------- Unique Save Utility ------------------------

def save_json_unique(data, folder, base_filename):
    counter = 1
    filename = os.path.join(folder, base_filename)
    while os.path.exists(filename):
        filename = os.path.join(folder, f"{base_filename.replace('.json','')}_{counter}.json")
        counter += 1
    with open(filename, 'w') as f:
        json.dump(data, f, indent=4)
    return filename

# --------------------- UID to MeSH Path Mapping -------------------------

def generate_uid_mesh_mapping(keys_and_classes):
    uid_to_path_and_term = {}

    def flatten(current_dict, current_path):
        for key, value in current_dict.items():
            if isinstance(value, dict):
                flatten(value, current_path + [key])
            else:
                uid_to_path_and_term[key] = (value, current_path)

    flatten(keys_and_classes, [])
    return uid_to_path_and_term
