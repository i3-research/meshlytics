"""
top_cited.py

This module builds a dataset of the Top N most cited papers published in a PubMed-indexed journal
over a chosen time window, ready for the `analyze` command:
- Journal lookup with spelling tolerance against the NLM journal list (essentials/pubmed_journals_*.json)
- Interactive confirmation of the matched journal (title, MedAbbr, ISSN, NLM ID) before any API call
- Ranked candidate list from the OpenAlex API (cited_by_count, descending, PubMed-indexed works only)
- PubMed screening with the same PublicationTypeList rules as `fetch` (reviews also excluded by default),
  backfilling from the ranked list so that exactly N papers remain in the final dataset
- PubMed cited-in count stored next to the OpenAlex citation count for comparison
- Full metadata via fetch.fetch_data_from_txt (MeSH terms, authors-institutions, abstract, journal, year)

Usage:
    meshlytics top-cited -j "lancet" -n 100
    meshlytics top-cited -j "CA Cancer J Clin" -n 100 --year-range 2016 2025

Outputs are saved in an auto-generated 'TopCited_<MedAbbr>_<from>_<to>_Top<N>_<date>' folder.
"""


import os
import re
import time
import unicodedata
import difflib
import json
from datetime import datetime

import pandas as pd
import requests
from Bio import Entrez
from ratelimit import limits, sleep_and_retry

from meshlytics import fetch


# -------------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------------

OPENALEX_BASE_URL = "https://api.openalex.org"

# Same review definition as fetch.py (review_paper = 'Yes')
REVIEW_PUBLICATION_TYPES = {"Review", "Systematic Review", "Scoping Review"}

# OpenAlex fields requested for each work (keeps responses small)
OPENALEX_WORK_FIELDS = "id,doi,display_name,publication_year,cited_by_count,ids,type"

PMID_URL_PATTERN = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", flags=re.IGNORECASE)
ISSN_PATTERN = re.compile(r"^\d{4}-?\d{3}[\dXx]$")


# =========================================================================
# SECTION 1: Journal lookup and user confirmation
# =========================================================================

def normalize_journal_name(text):
    """
    Normalizes a journal name for matching:
    lowercase, ASCII, '&' -> 'and', punctuation removed, leading 'the' removed, single spaces.
    """
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text.startswith("the "):
        text = text[4:]
    return text


def normalize_issn(text):
    """Returns ISSN as 'NNNN-NNNX' (upper case), or '' if not an ISSN."""
    text = str(text or "").strip()
    if not ISSN_PATTERN.match(text):
        return ""
    digits = text.replace("-", "").upper()
    return f"{digits[:4]}-{digits[4:]}"


def load_pubmed_journals(journals_path):
    """
    Loads the NLM journal list (essentials/pubmed_journals_*.json) and adds normalized
    name columns used for matching.

    Returns:
        pd.DataFrame with columns JournalTitle, MedAbbr, IsoAbbr, ISSN (Print), ISSN (Online), NlmId
        plus helper columns _norm_title, _norm_title_short, _norm_title_main, _norm_medabbr, _norm_isoabbr.
    """
    if not os.path.exists(journals_path):
        raise FileNotFoundError(f"PubMed journal list not found: {journals_path}")

    with open(journals_path, "r", encoding="utf-8") as f:
        journals = pd.DataFrame(json.load(f))

    for col in ["JournalTitle", "MedAbbr", "IsoAbbr", "ISSN (Print)", "ISSN (Online)", "NlmId"]:
        if col not in journals.columns:
            journals[col] = ""
        journals[col] = journals[col].fillna("").astype(str).str.strip()

    journals = journals[journals["MedAbbr"] != ""].reset_index(drop=True)

    journals["_norm_title"] = journals["JournalTitle"].map(normalize_journal_name)
    # Title without parenthetical place of publication, e.g. "Lancet (London, England)" -> "lancet"
    journals["_norm_title_short"] = journals["JournalTitle"].str.replace(r"\s*\(.*?\)\s*", " ", regex=True).map(normalize_journal_name)
    # Main title before a subtitle, e.g. "The Journal of neuroscience : the official journal of ..." -> "journal of neuroscience"
    journals["_norm_title_main"] = journals["JournalTitle"].str.split(r"\s[:=]\s|:\s", n=1, regex=True).str[0].str.replace(r"\s*\(.*?\)\s*", " ", regex=True).map(normalize_journal_name)
    journals["_norm_medabbr"] = journals["MedAbbr"].map(normalize_journal_name)
    journals["_norm_isoabbr"] = journals["IsoAbbr"].map(normalize_journal_name)

    return journals


def find_journal_matches(query, journals_df, max_matches=5, fuzzy_cutoff=0.6):
    """
    Finds the journals that best match a user-typed name, abbreviation or ISSN.

    Matching order:
      1. ISSN (print or online), if the query looks like an ISSN
      2. Exact match on normalized JournalTitle / title without place / main title / MedAbbr / IsoAbbr
      3. Prefix match (e.g. 'lancet' also lists 'Lancet Oncol', 'Lancet Neurol', ...)
      4. Fuzzy match (difflib ratio >= fuzzy_cutoff) to tolerate spelling errors
    If an exact match exists it is listed first, followed by its journal family (prefix matches);
    otherwise prefix and fuzzy matches are ranked together by score.

    Returns:
        pd.DataFrame of up to max_matches journals with columns 'match_type' and 'match_score',
        exact matches first.
    """
    name_cols = ["_norm_title", "_norm_title_short", "_norm_title_main", "_norm_medabbr", "_norm_isoabbr"]

    # --- 1. ISSN lookup ---
    issn = normalize_issn(query)
    if issn:
        hits = journals_df[(journals_df["ISSN (Print)"].str.upper() == issn) | (journals_df["ISSN (Online)"].str.upper() == issn)].copy()
        hits["match_type"] = "ISSN"
        hits["match_score"] = 1.0
        return hits.head(max_matches)

    q = normalize_journal_name(query)
    if not q:
        return journals_df.iloc[0:0].assign(match_type=[], match_score=[])

    scores = {}  # row index -> (priority, score, match_type); best match kept per journal

    def keep_best(idx, priority, score, match_type):
        current = scores.get(idx)
        if current is None or (priority, score) > (current[0], current[1]):
            scores[idx] = (priority, score, match_type)

    # --- 2. Exact matches ---
    for col in name_cols:
        for idx in journals_df.index[journals_df[col] == q]:
            keep_best(idx, 2, 1.0, "exact")

    # --- 3. Prefix matches (word boundary) ---
    if len(q) >= 3:
        for col in ["_norm_medabbr", "_norm_title_main"]:
            mask = journals_df[col].str.startswith(q + " ")
            for idx in journals_df.index[mask]:
                ratio = len(q) / max(len(journals_df.at[idx, col]), 1)
                keep_best(idx, 1, round(0.5 + 0.4 * ratio, 3), "prefix")

    # --- 4. Fuzzy matches (spelling errors) ---
    for col in name_cols:
        values = journals_df[col]
        # quick length filter before the more expensive ratio
        lengths = values.str.len()
        candidates = values[(lengths >= len(q) * 0.6) & (lengths <= len(q) * 1.6 + 2)]
        matcher = difflib.SequenceMatcher(autojunk=False)
        matcher.set_seq2(q)
        for idx, val in candidates.items():
            matcher.set_seq1(val)
            if matcher.real_quick_ratio() < fuzzy_cutoff or matcher.quick_ratio() < fuzzy_cutoff:
                continue
            ratio = matcher.ratio()
            if ratio >= fuzzy_cutoff:
                keep_best(idx, 1, round(ratio, 3), "fuzzy")

    if not scores:
        return journals_df.iloc[0:0].assign(match_type=[], match_score=[])

    # With an exact match: exact first, then its journal family (prefix), then fuzzy.
    # Without an exact match: rank prefix and fuzzy matches by score (spelling errors).
    has_exact = any(v[2] == "exact" for v in scores.values())
    type_order = {"exact": 0, "prefix": 1, "fuzzy": 2} if has_exact else {"exact": 0, "prefix": 1, "fuzzy": 1}
    ranked = sorted(scores.items(), key=lambda kv: (type_order[kv[1][2]], -kv[1][1], len(journals_df.at[kv[0], "JournalTitle"])))
    exact_count = sum(1 for _, v in ranked if v[2] in ("exact", "ISSN"))
    ranked = ranked[:max(max_matches, exact_count)]

    hits = journals_df.loc[[idx for idx, _ in ranked]].copy()
    hits["match_type"] = [v[2] for _, v in ranked]
    hits["match_score"] = [v[1] for _, v in ranked]
    return hits


def print_journal_matches(matches):
    """Prints a numbered table of candidate journals for the user to choose from."""
    print("\n  #   Match     Journal title                                         MedAbbr                     ISSN (Print / Online)   NLM ID")
    print("  " + "-" * 140)
    for i, (_, row) in enumerate(matches.iterrows(), start=1):
        issns = " / ".join([x for x in [row["ISSN (Print)"], row["ISSN (Online)"]] if x]) or "-"
        title = row["JournalTitle"][:52]
        print(f"  {i:<3} {row['match_type']:<9} {title:<53} {row['MedAbbr'][:27]:<27} {issns:<23} {row['NlmId']}")
    print()


def confirm_journal(query, journals_df, assume_yes=False, input_func=input, max_matches=5):
    """
    Shows the closest journal matches for the typed name and asks the user to confirm one.

    Parameters:
    query        : journal name / abbreviation / ISSN typed by the user
    journals_df  : output of load_pubmed_journals
    assume_yes   : non-interactive mode; accepts only a single exact (or ISSN) match, otherwise raises
    input_func   : function used to read user input (replaceable for testing)

    Returns:
    pd.Series row of the confirmed journal
    """
    while True:
        matches = find_journal_matches(query, journals_df, max_matches=max_matches)
        exact = matches[matches["match_type"].isin(["exact", "ISSN"])]

        if assume_yes:
            if len(exact) == 1:
                row = exact.iloc[0]
                print(f"[INFO] Journal matched (non-interactive): {row['JournalTitle']} | {row['MedAbbr']} | NLM ID {row['NlmId']}")
                return row
            if not matches.empty:
                print_journal_matches(matches)
            raise ValueError(
                f"Journal '{query}' has no single exact match ({len(exact)} exact, {len(matches)} close). "
                "Run without --yes to choose interactively, or pass the ISSN with --issn."
            )

        if matches.empty:
            print(f"[INFO] No journal in the PubMed journal list matches '{query}'.")
            query = input_func("Type the journal name again (or 'q' to quit): ").strip()
            if query.lower() in ("q", "quit", ""):
                raise SystemExit("[INFO] Journal selection cancelled by user.")
            continue

        if len(exact) == 1 and len(matches) == 1:
            print(f"\n[INFO] Matched journal for '{query}':")
        else:
            print(f"\n[INFO] Closest PubMed journals for '{query}':")
        print_journal_matches(matches)

        answer = input_func(f"Select journal [1-{len(matches)}] (Enter = 1), 'r' to retype, 'q' to quit: ").strip().lower()

        if answer in ("q", "quit"):
            raise SystemExit("[INFO] Journal selection cancelled by user.")
        if answer in ("r", "retype"):
            query = input_func("Type the journal name: ").strip()
            continue
        if answer == "":
            answer = "1"
        if answer.isdigit() and 1 <= int(answer) <= len(matches):
            row = matches.iloc[int(answer) - 1]
            print(f"[INFO] Confirmed journal: {row['JournalTitle']} | {row['MedAbbr']} | NLM ID {row['NlmId']}")
            return row

        print(f"[INFO] '{answer}' is not a valid choice.")


# =========================================================================
# SECTION 2: OpenAlex ranked candidates
# =========================================================================

def openalex_get(endpoint, params, openalex_api_key, max_retries=4, timeout=60):
    """
    GET request to the OpenAlex API with the API key and retry on rate limit / server errors.

    Returns:
        (status_code, json or None)
    """
    url = f"{OPENALEX_BASE_URL}/{endpoint.lstrip('/')}"
    params = dict(params or {})
    params["api_key"] = openalex_api_key

    base_wait = 2
    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            wait_time = base_wait * (2 ** attempt)
            print(f"[INFO] OpenAlex connection error ({type(e).__name__}), retry {attempt + 1}/{max_retries} in {wait_time}s")
            time.sleep(wait_time)
            continue

        if response.status_code == 200:
            return 200, response.json()
        if response.status_code == 404:
            return 404, None
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"OpenAlex rejected the request (HTTP {response.status_code}). "
                "Check OPENALEX_API_KEY or 'openalex_api_key' in the config file."
            )
        if response.status_code == 429 or response.status_code >= 500:
            wait_time = base_wait * (2 ** attempt)
            print(f"[INFO] OpenAlex HTTP {response.status_code}, retry {attempt + 1}/{max_retries} in {wait_time}s")
            time.sleep(wait_time)
            continue

        raise RuntimeError(f"OpenAlex request failed (HTTP {response.status_code}): {response.text[:300]}")

    raise RuntimeError(f"OpenAlex request failed after {max_retries} attempts: {url}")


def resolve_openalex_source(journal_row, openalex_api_key, assume_yes=False, input_func=input):
    """
    Finds the OpenAlex source (journal) for the confirmed PubMed journal.
    Uses the ISSNs first (exact); falls back to an OpenAlex name search with user confirmation
    when the journal has no ISSN in the NLM list or OpenAlex does not know the ISSN.

    Returns:
        dict with keys id, display_name, issn_l, issn, works_count, matched_by
    """
    source_fields = "id,display_name,issn_l,issn,works_count,type"

    # --- ISSN lookup (print, then online) ---
    for issn in [journal_row["ISSN (Print)"], journal_row["ISSN (Online)"]]:
        issn = normalize_issn(issn)
        if not issn:
            continue
        status, data = openalex_get(f"sources/issn:{issn}", {"select": source_fields}, openalex_api_key)
        if status == 200 and data and data.get("id"):
            data["matched_by"] = f"ISSN {issn}"
            print(f"[INFO] OpenAlex source: {data.get('display_name')} ({data['id']}), matched by ISSN {issn}, works_count={data.get('works_count')}")
            return data

    # --- Name search fallback ---
    print(f"[INFO] No OpenAlex source found by ISSN for '{journal_row['JournalTitle']}'. Searching OpenAlex by name...")
    status, data = openalex_get(
        "sources",
        {"search": journal_row["JournalTitle"], "filter": "type:journal", "per_page": 5, "select": source_fields},
        openalex_api_key,
    )
    results = (data or {}).get("results", [])
    if not results:
        raise RuntimeError(f"OpenAlex has no journal source matching '{journal_row['JournalTitle']}'.")

    if assume_yes:
        raise ValueError(
            "OpenAlex source could only be found by name search, which needs confirmation. Run without --yes."
        )

    print("\n  #   OpenAlex source                                              ISSN-L       Works")
    print("  " + "-" * 90)
    for i, src in enumerate(results, start=1):
        print(f"  {i:<3} {str(src.get('display_name'))[:60]:<60} {str(src.get('issn_l') or '-'):<12} {src.get('works_count')}")
    answer = input_func(f"\nSelect OpenAlex source [1-{len(results)}] (Enter = 1), 'q' to quit: ").strip().lower()
    if answer in ("q", "quit"):
        raise SystemExit("[INFO] OpenAlex source selection cancelled by user.")
    choice = int(answer) if answer.isdigit() and 1 <= int(answer) <= len(results) else 1
    source = results[choice - 1]
    source["matched_by"] = "name search (user confirmed)"
    print(f"[INFO] OpenAlex source: {source.get('display_name')} ({source['id']})")
    return source


def extract_pmid(value):
    """Extracts PMID digits from an OpenAlex ids.pmid URL (e.g. https://pubmed.ncbi.nlm.nih.gov/33885740)."""
    if value is None:
        return None
    match = PMID_URL_PATTERN.search(str(value))
    if match:
        return match.group(1)
    value = str(value).strip()
    return value if value.isdigit() else None


def iter_openalex_top_works(source_id, from_year, to_year, openalex_api_key, per_page=100):
    """
    Generator over the works of one OpenAlex source in the year window, most cited first.
    Only works with a PMID are requested (has_pmid:true). Duplicate PMIDs are skipped.

    Yields:
        dict with openalex_rank, pmid, openalex_id, openalex_title, openalex_year,
        openalex_type, openalex_cited_by_count, doi
    """
    source_key = str(source_id).rstrip("/").split("/")[-1]  # 'https://openalex.org/S123' -> 'S123'
    params = {
        "filter": f"primary_location.source.id:{source_key},publication_year:{from_year}-{to_year},has_pmid:true",
        "sort": "cited_by_count:desc",
        "per_page": per_page,
        "select": OPENALEX_WORK_FIELDS,
        "cursor": "*",
    }

    rank = 0
    seen_pmids = set()
    first_page = True

    while params.get("cursor"):
        status, data = openalex_get("works", params, openalex_api_key)
        if status != 200 or not data:
            raise RuntimeError(f"OpenAlex works request failed for source {source_key} (HTTP {status}).")

        if first_page:
            total = data.get("meta", {}).get("count")
            print(f"[INFO] OpenAlex: {total} PubMed-indexed works in {source_key} for {from_year}-{to_year}")
            first_page = False

        results = data.get("results", [])
        if not results:
            return

        for work in results:
            pmid = extract_pmid((work.get("ids") or {}).get("pmid"))
            if not pmid or pmid in seen_pmids:
                continue
            seen_pmids.add(pmid)
            rank += 1
            yield {
                "openalex_rank": rank,
                "pmid": pmid,
                "openalex_id": work.get("id"),
                "openalex_title": work.get("display_name"),
                "openalex_year": work.get("publication_year"),
                "openalex_type": work.get("type"),
                "openalex_cited_by_count": work.get("cited_by_count"),
                "doi": work.get("doi"),
            }

        params["cursor"] = data.get("meta", {}).get("next_cursor")


# =========================================================================
# SECTION 3: PubMed screening (backfill to exactly N) and cited-in counts
# =========================================================================

def fetch_pubmed_records_batch(pmids, calls_per_second):
    """
    Uses one Entrez.efetch call for a batch of PMIDs.

    Returns:
        dict {pmid: PubmedArticle record}; PMIDs that PubMed did not return are absent.
    """

    @sleep_and_retry
    @limits(calls=calls_per_second, period=1)
    def limited_fetch_batch():
        for attempt in range(3):
            try:
                handle = Entrez.efetch(db="pubmed", id=",".join(pmids), retmode="xml")
                records = Entrez.read(handle)
                handle.close()
                return records
            except Exception as e:
                print(f"[ERROR] PubMed batch efetch attempt {attempt + 1}/3: {type(e).__name__} - {e}")
                time.sleep(3 * (attempt + 1))
        return None

    records = limited_fetch_batch()
    articles = {}
    if records is None:
        return articles
    for article in records.get("PubmedArticle", []):
        pmid = str(article.get("MedlineCitation", {}).get("PMID", "")).strip()
        if pmid:
            articles[pmid] = article
    return articles


def screen_pubmed_record(article, journal_row, include_reviews=False):
    """
    Applies the dataset rules to one PubMed record.

    Rules (in order):
      - non-original publication types (fetch.EXCLUDE_PUBLICATION_TYPES) are excluded
      - review papers are excluded unless include_reviews=True
      - the PubMed journal (NlmUniqueID) must be the confirmed journal

    Returns:
        dict with keep (bool), reason, pub_types, pubmed_year, pubmed_journal
    """
    pub_types = fetch.get_publication_types(article)
    citation = article.get("MedlineCitation", {})
    journal_info = citation.get("MedlineJournalInfo", {})
    nlm_id = str(journal_info.get("NlmUniqueID", "")).strip()
    pubmed_journal = str(journal_info.get("MedlineTA", "")).strip()
    pubmed_year = fetch.get_publication_year(article)

    result = {
        "keep": True,
        "reason": None,
        "pub_types": "; ".join(pub_types),
        "pubmed_year": str(pubmed_year) if pubmed_year else None,
        "pubmed_journal": pubmed_journal,
    }

    exclude_rec, exclude_hits = fetch.should_exclude_by_publication_type(pub_types)
    if exclude_rec:
        result.update(keep=False, reason=f"Excluded publication type: {', '.join(exclude_hits)}")
    elif not include_reviews and any(t in REVIEW_PUBLICATION_TYPES for t in pub_types):
        result.update(keep=False, reason="Review paper")
    elif journal_row["NlmId"] and nlm_id and nlm_id != journal_row["NlmId"]:
        result.update(keep=False, reason=f"Journal mismatch in PubMed ({pubmed_journal}, NLM ID {nlm_id})")

    return result


def select_top_n_pubmed(candidates, top_n, journal_row, calls_per_second, include_reviews=False, batch_size=100):
    """
    Walks the OpenAlex ranked candidates in order, screens them in PubMed batches and keeps
    the first top_n that pass the rules. Excluded papers are replaced by the next-ranked ones,
    so the padding adapts to how many papers each journal loses.

    Returns:
        (selected: list[dict], ranking_log: list[dict], exhausted: bool)
    """
    selected = []
    ranking_log = []
    exhausted = False
    candidates = iter(candidates)

    while len(selected) < top_n:
        batch = []
        for cand in candidates:
            batch.append(cand)
            if len(batch) >= batch_size:
                break
        if not batch:
            exhausted = True
            break

        print(f"[INFO] Screening OpenAlex ranks {batch[0]['openalex_rank']}-{batch[-1]['openalex_rank']} in PubMed "
              f"({len(selected)}/{top_n} selected so far)")
        articles = fetch_pubmed_records_batch([c["pmid"] for c in batch], calls_per_second)

        for cand in batch:
            entry = dict(cand)
            if len(selected) >= top_n:
                entry.update(status="not needed", reason="Top N already reached")
                ranking_log.append(entry)
                continue

            article = articles.get(cand["pmid"])
            if article is None:
                entry.update(status="excluded", reason="Not returned by PubMed efetch")
                ranking_log.append(entry)
                continue

            screen = screen_pubmed_record(article, journal_row, include_reviews=include_reviews)
            entry.update(pub_types=screen["pub_types"], pubmed_year=screen["pubmed_year"], pubmed_journal=screen["pubmed_journal"])
            if screen["keep"]:
                entry.update(status="included", reason=None, top_cited_rank=len(selected) + 1)
                selected.append(entry)
            else:
                entry.update(status="excluded", reason=screen["reason"])
            ranking_log.append(entry)

    return selected, ranking_log, exhausted


def get_pubmed_cited_in_counts(pmids, calls_per_second, batch_size=100):
    """
    Counts PubMed 'cited in' links (pubmed_pubmed_citedin) for a list of PMIDs, in batches.
    Note: PubMed only counts citing papers with PMC full text, so this is lower than OpenAlex.

    Returns:
        dict {pmid: count or None if the request failed}
    """

    @sleep_and_retry
    @limits(calls=calls_per_second, period=1)
    def limited_elink(batch):
        handle = Entrez.elink(dbfrom="pubmed", id=batch, linkname="pubmed_pubmed_citedin")
        record = Entrez.read(handle)
        handle.close()
        return record

    counts = {}
    for start in range(0, len(pmids), batch_size):
        batch = [str(p) for p in pmids[start:start + batch_size]]
        try:
            record = limited_elink(batch)
        except Exception as e:
            print(f"[ERROR] PubMed cited-in batch failed: {type(e).__name__} - {e}")
            counts.update({p: None for p in batch})
            continue

        for linkset in record:
            ids = linkset.get("IdList", [])
            if not ids:
                continue
            pmid = str(ids[0])
            n_links = 0
            for linkset_db in linkset.get("LinkSetDb", []):
                if linkset_db.get("LinkName") == "pubmed_pubmed_citedin":
                    n_links = len(linkset_db.get("Link", []))
            counts[pmid] = n_links

        for p in batch:
            counts.setdefault(p, None)

    return counts


# =========================================================================
# SECTION 4: Output helpers
# =========================================================================

def resolve_year_window(years_back=10, year_range=None):
    """
    Returns (from_year, to_year).
    Default: the last `years_back` complete years (current year excluded, it is still ongoing).
    """
    if year_range is not None:
        if len(year_range) != 2:
            raise ValueError("year_range must be two years: FROM TO")
        from_year, to_year = map(int, year_range)
        if from_year > to_year:
            raise ValueError(f"Invalid year_range: start year {from_year} > end year {to_year}")
        return from_year, to_year

    if years_back is None or int(years_back) <= 0:
        raise ValueError(f"years_back must be > 0, got {years_back}")
    to_year = datetime.now().year - 1
    from_year = to_year - int(years_back) + 1
    return from_year, to_year


def create_top_cited_output_dir(med_abbr, from_year, to_year, top_n, base_dir=None):
    """Creates 'TopCited_<MedAbbr>_<from>_<to>_Top<N>_<MonDD_YYYY>' (in base_dir or the current folder)."""
    safe_abbr = re.sub(r"[^A-Za-z0-9]+", "_", med_abbr).strip("_")
    today = datetime.today().strftime("%b%d_%Y")
    folder = f"TopCited_{safe_abbr}_{from_year}_{to_year}_Top{top_n}_{today}"
    if base_dir:
        folder = os.path.join(base_dir, folder)
    os.makedirs(folder, exist_ok=True)
    return folder, f"{safe_abbr}_{from_year}_{to_year}_Top{top_n}"


def add_citation_columns_to_fetched_data(fetched_xlsx, selected):
    """
    Adds the ranking/citation columns to the fetched data Excel file (same file, same sheet),
    keeps the top-cited order and appends the new columns at the end so `analyze` is unaffected.
    """
    df = pd.read_excel(fetched_xlsx)
    df["pubmed_id"] = df["pubmed_id"].astype(str)

    extra = pd.DataFrame(selected)[[
        "pmid", "top_cited_rank", "openalex_rank", "openalex_cited_by_count",
        "pubmed_cited_in_count", "openalex_id", "openalex_year",
    ]].rename(columns={"pmid": "pubmed_id"})
    extra["pubmed_id"] = extra["pubmed_id"].astype(str)

    merged = df.merge(extra, on="pubmed_id", how="left").sort_values("top_cited_rank", kind="mergesort")
    merged.to_excel(fetched_xlsx, index=False)
    return merged


def write_top_cited_summary(summary_path, run_info, counts):
    """Appends (or creates) a run-stamped summary block, same layout as analysis_summary.txt."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "a" if os.path.exists(summary_path) else "w"
    with open(summary_path, mode, encoding="utf-8") as f:
        if mode == "a":
            f.write("\n" + ("=" * 80) + "\n")
        f.write(f"RUN @ {ts}\n")
        f.write("\n=== RUN PARAMETERS ===\n")
        for k, v in run_info.items():
            f.write(f"{k}: {v}\n")
        f.write("\n=== SELECTION SUMMARY ===\n")
        for k, v in counts.items():
            f.write(f"{k}: {v}\n")


# =========================================================================
# SECTION 5: Main run
# =========================================================================

def run_top_cited(
    journal_query,
    email,
    api_key,
    openalex_api_key,
    mesh_dictionary_path,
    journals_path,
    top_n=100,
    years_back=10,
    year_range=None,
    include_reviews=False,
    assume_yes=False,
    output_dir=None,
    input_func=input,
    use_mesh_on_demand=False,
):
    """
    Builds the Top N most cited papers dataset for one PubMed-indexed journal.

    Parameters:
    journal_query        : journal name / abbreviation (spelling errors tolerated) or ISSN
    email, api_key       : NCBI E-utilities contact email and API key (see config.py)
    openalex_api_key     : OpenAlex API key (see config.py)
    mesh_dictionary_path : MeSH dictionary (for mesh-count and the optional MeSH on Demand step)
    journals_path        : essentials/pubmed_journals_*.json
    top_n                : number of papers in the final dataset (default 100)
    years_back           : last N complete years (default 10), ignored if year_range is given
    year_range           : (from_year, to_year), inclusive
    include_reviews      : keep review papers (default False: excluded and backfilled)
    assume_yes           : non-interactive journal confirmation (single exact match only)
    output_dir           : parent folder for the output folder (default: current folder)
    use_mesh_on_demand   : request MeSH on Demand terms for papers not yet indexed (experimental, default off)

    Returns:
    Path to the fetched data Excel file (input for `analyze`)
    """
    if top_n is None or int(top_n) <= 0:
        raise ValueError(f"top_n must be > 0, got {top_n}")
    top_n = int(top_n)
    if not openalex_api_key:
        raise RuntimeError("Top cited analysis requires an OpenAlex API key. Set OPENALEX_API_KEY or 'openalex_api_key' in the config file.")

    # --- Entrez setup (same limits as fetch.py) ---
    if email:
        Entrez.email = email
    Entrez.tool = fetch.ENTREZ_TOOL
    if api_key:
        Entrez.api_key = api_key
        calls_per_second = 10  # max allowed by NCBI with API key
    else:
        calls_per_second = 3   # max allowed without API key
    print(f"\n[INFO] PubMed max calls per sec = {calls_per_second}")

    from_year, to_year = resolve_year_window(years_back=years_back, year_range=year_range)
    print(f"[INFO] Publication window: {from_year}-{to_year} | Top N = {top_n} | Reviews {'included' if include_reviews else 'excluded (backfilled)'}")

    # --- Step 1: journal confirmation ---
    journals_df = load_pubmed_journals(journals_path)
    journal_row = confirm_journal(journal_query, journals_df, assume_yes=assume_yes, input_func=input_func)

    # --- Step 2: OpenAlex source and ranked candidates ---
    source = resolve_openalex_source(journal_row, openalex_api_key, assume_yes=assume_yes, input_func=input_func)
    candidates = iter_openalex_top_works(source["id"], from_year, to_year, openalex_api_key)

    # --- Step 3: PubMed screening with backfill ---
    selected, ranking_log, exhausted = select_top_n_pubmed(
        candidates, top_n, journal_row, calls_per_second, include_reviews=include_reviews
    )
    if not selected:
        raise RuntimeError("No papers passed the PubMed screening; nothing to fetch.")
    if exhausted and len(selected) < top_n:
        print(f"[INFO] Only {len(selected)} papers available after screening (requested {top_n}); continuing with {len(selected)}.")

    excluded = [r for r in ranking_log if r.get("status") == "excluded"]
    print(f"[SUMMARY] Candidates screened: {sum(1 for r in ranking_log if r.get('status') != 'not needed')} | "
          f"Excluded: {len(excluded)} | Selected: {len(selected)}")

    # --- Step 4: PubMed cited-in counts for the selected papers ---
    print("[INFO] Getting PubMed cited-in counts for selected papers...")
    cited_in = get_pubmed_cited_in_counts([s["pmid"] for s in selected], calls_per_second)
    for s in selected:
        s["pubmed_cited_in_count"] = cited_in.get(s["pmid"])
    for r in ranking_log:
        if r.get("status") == "included":
            r["pubmed_cited_in_count"] = cited_in.get(r["pmid"])

    # --- Step 5: outputs ---
    out_dir, prefix = create_top_cited_output_dir(journal_row["MedAbbr"], from_year, to_year, top_n, base_dir=output_dir)

    ranking_cols = [
        "openalex_rank", "top_cited_rank", "status", "reason", "pmid", "openalex_title",
        "openalex_year", "pubmed_year", "openalex_type", "pub_types", "pubmed_journal",
        "openalex_cited_by_count", "pubmed_cited_in_count", "doi", "openalex_id",
    ]
    ranking_df = pd.DataFrame(ranking_log).reindex(columns=ranking_cols)

    run_info = {
        "journal_query": journal_query,
        "journal_title": journal_row["JournalTitle"],
        "med_abbr": journal_row["MedAbbr"],
        "issn_print": journal_row["ISSN (Print)"],
        "issn_online": journal_row["ISSN (Online)"],
        "nlm_id": journal_row["NlmId"],
        "openalex_source_id": source.get("id"),
        "openalex_source_name": source.get("display_name"),
        "openalex_matched_by": source.get("matched_by"),
        "from_year": from_year,
        "to_year": to_year,
        "top_n_requested": top_n,
        "include_reviews": include_reviews,
        "ranking_metric": "OpenAlex cited_by_count (descending)",
        "run_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    counts = {
        "candidates_screened": int((ranking_df["status"] != "not needed").sum()),
        "excluded_total": len(excluded),
        "excluded_reviews": sum(1 for r in excluded if r.get("reason") == "Review paper"),
        "excluded_publication_type": sum(1 for r in excluded if str(r.get("reason", "")).startswith("Excluded publication type")),
        "excluded_journal_mismatch": sum(1 for r in excluded if str(r.get("reason", "")).startswith("Journal mismatch")),
        "excluded_not_in_pubmed": sum(1 for r in excluded if r.get("reason") == "Not returned by PubMed efetch"),
        "selected": len(selected),
        "lowest_selected_openalex_rank": selected[-1]["openalex_rank"],
        "candidate_list_exhausted": exhausted,
    }

    ranking_path = os.path.join(out_dir, f"{prefix}_ranking.xlsx")
    with pd.ExcelWriter(ranking_path) as writer:
        ranking_df.to_excel(writer, sheet_name="Ranking", index=False)
        pd.DataFrame(list({**run_info, **counts}.items()), columns=["parameter", "value"]).to_excel(writer, sheet_name="Run_Info", index=False)
    print(f"[INFO] Ranking with inclusion/exclusion reasons saved to: {ranking_path}")

    pmids_txt = os.path.join(out_dir, f"{prefix}_pmids.txt")
    with open(pmids_txt, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(s["pmid"] for s in selected) + "\n")
    print(f"[INFO] Ranked PMID list saved to: {pmids_txt}")

    # --- Step 6: full metadata with the existing fetch (MeSH, authors, abstract, ...) ---
    print(f"\n[INFO] Fetching full PubMed metadata for {len(selected)} selected papers...")
    fetch.fetch_data_from_txt(pmids_txt, email, api_key, mesh_dictionary_path, use_mesh_on_demand=use_mesh_on_demand)
    fetched_xlsx = os.path.splitext(pmids_txt)[0] + "_fetched_data.xlsx"

    merged = add_citation_columns_to_fetched_data(fetched_xlsx, selected)
    counts["rows_in_fetched_data"] = len(merged)
    if len(merged) != len(selected):
        print(f"[INFO] Fetched data has {len(merged)} rows for {len(selected)} selected PMIDs; see the skipped PMIDs log.")

    write_top_cited_summary(os.path.join(out_dir, "top_cited_summary.txt"), run_info, counts)

    print(f"\n[SUMMARY] Top {len(selected)} most cited papers of {journal_row['MedAbbr']} ({from_year}-{to_year}) saved to:")
    print(f"{fetched_xlsx}")
    print(f"Run analysis with: meshlytics analyze -f \"{fetched_xlsx}\" [options]")
    return fetched_xlsx


if __name__ == "__main__":
    print("This script is part of the Meshlytics CLI and is not meant to be run directly.")
    print("Use case: eg meshlytics top-cited -j \"lancet\" -n 100")
