"""
filter_data.py

Filters MeSH terms from citation Excel files using keyword rules.
Can be invoked via CLI as part of the Meshlytics tool.

Usage:
    meshlytics filter-data -f input_file.xlsx [-k exclusion_terms_file] [--exclude-categories A Z V]

If -k is omitted, the keyword file (mesh_terms_with_keys_and_counts*.xlsx) beside the input file is used
(preferring one with terms marked in 'filter_out_term?'). The filtered file is saved beside the input file
as <input name>_filtered.xlsx unless -o is given.
"""


import pandas as pd
import re
import ast
import os
from meshlytics.utils import find_keyword_file, FILTER_YES_VALUES


EXCLUDE_YES_VALUES = FILTER_YES_VALUES  # compared in lower case

def safe_literal_eval(val):
    try:
        val = val.replace('\n', '').strip()
        return ast.literal_eval(val) if val.startswith('(') and val.endswith(')') else []
    except (SyntaxError, ValueError):
        return []

def filter_mesh_terms(mesh_term_string, keywords):
    tuples = re.findall(r"\('([^']*)','([^']*)'\)", mesh_term_string)
    filtered_tuples = [t for t in tuples if t[0] not in keywords]
    cleaned_string = ",".join([f"('{t[0]}','{t[1]}')" for t in filtered_tuples])
    return cleaned_string, len(filtered_tuples)

CODE_RE = re.compile(r"(?:^|>)\s*([A-Z](?:\d{2}(?:\.\d{3})*)?)\s*\(")


def path_codes(path):
    """'A (Anatomy) > A08 (Nervous System) > A08.186 (...)' -> ['A', 'A08', 'A08.186']"""
    return CODE_RE.findall(str(path))


def term_in_categories(path_with_keys, categories, match="all"):
    """
    True if a term should be removed for the given MeSH categories.
    path_with_keys holds every tree path of the term, ' | ' separated (older keyword files hold one path).
    match='all' : remove only if EVERY path lies in an excluded branch (a term also indexed elsewhere is kept)
    match='any' : remove if ANY path lies in an excluded branch
    """
    paths = [p for p in str(path_with_keys).split(" | ") if path_codes(p)]
    if not paths or not categories:
        return False
    hits = [any(c in path_codes(p) for c in categories) for p in paths]
    return all(hits) if match == "all" else any(hits)


def get_keywords_to_filter(key_path_or_excel, exclude_categories, special_terms, category_match="all"):
    if key_path_or_excel.endswith('.xlsx'):
        df = pd.read_excel(key_path_or_excel)

        terms_to_exclude = df[
            df['filter_out_term?'].astype(str).str.lower().isin(EXCLUDE_YES_VALUES)
        ]['Term'].tolist()

        z_terms = []
        categories = [c.strip().upper() for c in exclude_categories if c.strip()]
        if 'Path with Keys' in df.columns and categories:
            mask = df['Path with Keys'].apply(lambda p: term_in_categories(p, categories, category_match))
            z_terms = df[mask]['Term'].tolist()
            kept_multi = df[~mask & df['Path with Keys'].apply(lambda p: term_in_categories(p, categories, "any"))]['Term'].tolist()
            print(f"[INFO] --exclude-categories {' '.join(categories)} (match = {category_match}): {len(z_terms)} terms removed")
            if category_match == "all" and kept_multi:
                print(f"[INFO] {len(kept_multi)} terms kept because they also sit in other branches "
                      f"(use --category-match any to remove them): {', '.join(kept_multi[:15])}{' ...' if len(kept_multi) > 15 else ''}")

        z_terms = [t for t in z_terms if t not in terms_to_exclude]
        return set(terms_to_exclude + z_terms + special_terms)

    else:
        with open(key_path_or_excel, 'r') as f:
            return set([line.strip() for line in f if line.strip()])

def is_keyword_file_marked(path):
    """True if any term is marked in 'filter_out_term?'."""
    from meshlytics.utils import is_edited_keyword_file
    return is_edited_keyword_file(path)


def filter_excel(input_file, keywords):
    workbook = pd.ExcelFile(input_file)
    sheet_names = workbook.sheet_names
    sheet_dict = {}

    for sheet in sheet_names:
        df = pd.read_excel(workbook, sheet_name=sheet)
        if 'mesh_terms' in df.columns:
            df[['mesh_terms', 'mesh_term_count']] = df.apply(
                lambda x: filter_mesh_terms(x['mesh_terms'], keywords) if pd.notnull(x['mesh_terms']) else (x['mesh_terms'], 0),
                axis=1,
                result_type='expand'
            )
        sheet_dict[sheet] = df

    return sheet_dict, sheet_names

def resolve_filter_paths(input_file, keywords_path=None, output=None):
    """
    Resolves the keyword file and output path for filter-data.
    - keywords_path None  -> keyword file beside the input file (see find_keyword_file)
    - output None         -> <input folder>/<input name>_filtered.xlsx
    - output without folder (e.g. 'my_filtered.xlsx') -> saved beside the input file
    - output with a folder -> used as given

    Returns:
        (keywords_path, output_path)
    """
    input_dir = os.path.dirname(os.path.abspath(input_file))

    if keywords_path is None:
        keywords_path, others = find_keyword_file(input_file)
        if keywords_path is None:
            raise FileNotFoundError(
                f"No keyword file (mesh_terms_with_keys_and_counts*.xlsx) found beside {input_file}.\n"
                f"  Create one with: meshlytics mesh-count -f \"{input_file}\"\n"
                f"  mark terms in 'filter_out_term?', then rerun filter-data (or pass the keyword file with -k)."
            )
        print(f"[INFO] Keyword file (auto-detected): {keywords_path}")
        if others:
            print(f"[INFO] Other keyword files in the folder (not used): {', '.join(os.path.basename(o) for o in others)}")
    elif not os.path.exists(keywords_path):
        raise FileNotFoundError(f"Keyword file not found: {keywords_path}")

    if output is None:
        stem = os.path.splitext(os.path.basename(input_file))[0]
        output = os.path.join(input_dir, f"{stem}_filtered.xlsx")
    elif not os.path.dirname(output):
        output = os.path.join(input_dir, output)

    return keywords_path, output


def run_filter_data(args):
    """
    Run MeSH term filtering using arguments passed from the CLI.

    Args:
    Namespace object from argparse with attributes:
    - file
    - keywords (optional; auto-detected beside the input file if None)
    - output   (optional; <input name>_filtered.xlsx beside the input file if None)
    - exclude_categories
    - special_terms
    - category_match ('all' = remove a term only if all its tree paths are in excluded branches; 'any')
    """

    args.keywords, args.output = resolve_filter_paths(args.file, args.keywords, args.output)
    if args.keywords.endswith('.xlsx') and not is_keyword_file_marked(args.keywords) and not args.exclude_categories and not args.special_terms:
        print(f"[INFO] No terms are marked in 'filter_out_term?' of {os.path.basename(args.keywords)} and no categories/special terms were given: the output will equal the input.")

    keywords = get_keywords_to_filter(args.keywords, args.exclude_categories, args.special_terms,
                                      getattr(args, 'category_match', 'all'))
    print(f"[INFO] The following MeSH terms are getting filtered out of mesh terms ... \n[{keywords}]")
    sheet_dict, sheet_order = filter_excel(args.file, keywords)

    with pd.ExcelWriter(args.output) as writer:
        for sheet in sheet_order:
            sheet_dict[sheet].to_excel(writer, sheet_name=sheet, index=False)

    print(f"[INFO] Filtered data saved to: {args.output}")

if __name__ == "__main__":
    print("This script is part of the Meshlytics CLI and is not meant to be run directly.")
    print("Use case: eg meshlytics filter-data -f data.xlsx -k analyzed_terms.xlsx --exclude-categories Z V")
