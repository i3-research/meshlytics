"""
fetch.py

This module is responsible for fetching citation data and metadata from the PubMed database using Entrez APIs.
It retrieves citations for given cohort papers and extracts details (title, authors, MeSH terms, etc.)
for each citing paper. Results are stored in an Excel workbook with one sheet per cohort.
"""


import pandas as pd
from Bio import Entrez
from ratelimit import limits, sleep_and_retry
import time
import os
from meshlytics.utils import (
    mesh_on_demand,
    get_mesh_tuples_from_terms,
    update_cohort_citation_counts,
    get_safe_keyword_file_path,
    get_fetch_logger,
    close_fetch_logger,
)
from tqdm import tqdm
import re
from collections import Counter
import json
import unicodedata

# -------------------------------------------------------------------------
# PubMed publication type filtering
# -------------------------------------------------------------------------
# We rely ONLY on PubMed's official PublicationTypeList tags (not title heuristics).
# Exclude: commentary/updates/corrections and integrity notices that are not "original" papers.
EXCLUDE_PUBLICATION_TYPES = {
    # Commentary / non-original scholarly context
    "Editorial",
    "Comment",
    "Letter",
    "Interview",
    "Lecture",
    "News",
    "Newspaper Article",
    "Personal Narrative",
    "Portrait",
    "Festschrift",
    "Historical Article",
    "Introductory Journal Article",
    "Congress",
    "Conference Proceedings",     # MeSH 2026 name of "Congress"
    "Patient Education Handout",
    "Interactive Tutorial",
    "Video-Audio Media",
    "Webcast",
    # Corrections / integrity / duplicates
    "Published Erratum",
    "Corrected and Republished Article",
    "Expression of Concern",
    "Retracted Publication",
    "Retraction of Publication",
    "Retraction Notice",          # current PubMed name of "Retraction of Publication"
    "Duplicate Publication",
}

# These are "metadata-ish" tags that often appear alongside real publication types.
# They should NOT be used to classify the main type of the article.
IGNORE_PUBLICATION_TYPES_FOR_PRIMARY = {
    "Research Support, American Recovery and Reinvestment Act",
    "Research Support, N.I.H., Extramural",
    "Research Support, N.I.H., Intramural",
    "Research Support, Non-U.S. Gov't",
    "Research Support, U.S. Gov't, Non-P.H.S.",
    "Research Support, U.S. Gov't, P.H.S.",
    "Research Support, U.S. Gov't",
    "Overall",
    "English Abstract",
    "Electronic Supplementary Materials",
}

def get_publication_types(article_record) -> list[str]:
    """Safely extract PubMed PublicationTypeList as a list[str]."""
    pub_types = (
        article_record.get("MedlineCitation", {})
        .get("Article", {})
        .get("PublicationTypeList")
    )
    if not pub_types:
        return []
    # Biopython elements stringify cleanly
    return [str(x).strip() for x in pub_types if str(x).strip()]

def choose_primary_publication_type(pub_types: list[str]) -> str | None:
    """Pick a single 'primary' publication type to store in the output."""
    if not pub_types:
        return None
    for t in pub_types:
        if t not in IGNORE_PUBLICATION_TYPES_FOR_PRIMARY:
            return t
    # Fallback: everything is ignorable -> take first
    return pub_types[0]

def should_exclude_by_publication_type(pub_types: list[str]) -> tuple[bool, list[str]]:
    """Return (exclude?, matched_exclude_types)."""
    hits = [t for t in pub_types if t in EXCLUDE_PUBLICATION_TYPES]
    return (len(hits) > 0), hits


def get_publication_year(article_record) -> str | None:
    """
    Publication year of a PubMed record: the journal issue year (PubDate/Year), else the first year in
    PubDate/MedlineDate (journals with issue dates such as '2023 Nov-Dec'), else the electronic publication
    year (ArticleDate). None if the record has no year.
    """
    article = article_record.get("MedlineCitation", {}).get("Article", {})
    pub_date = article.get("Journal", {}).get("JournalIssue", {}).get("PubDate", {}) or {}
    if pub_date.get("Year"):
        return str(pub_date["Year"]).strip()
    match = re.search(r"\b(1[89]\d{2}|2\d{3})\b", str(pub_date.get("MedlineDate", "")))
    if match:
        return match.group(1)
    for article_date in article.get("ArticleDate", []) or []:
        if article_date.get("Year"):
            return str(article_date["Year"]).strip()
    return None


def get_doi(article_record) -> str | None:
    """
    Safely extract the DOI of a PubMed record.

    Looks in MedlineCitation > Article > ELocationID first, then falls back to
    PubmedData > ArticleIdList. Returns None (instead of raising) when the record
    has no DOI, so a single record without ELocationID cannot abort a cohort fetch.
    """
    elocation_ids = (
        article_record.get("MedlineCitation", {})
        .get("Article", {})
        .get("ELocationID")
    ) or []
    for element in elocation_ids:
        attrs = getattr(element, "attributes", {}) or {}
        if attrs.get("EIdType") == "doi" and str(element).strip():
            return str(element).strip()

    article_ids = article_record.get("PubmedData", {}).get("ArticleIdList") or []
    for element in article_ids:
        attrs = getattr(element, "attributes", {}) or {}
        if attrs.get("IdType") == "doi" and str(element).strip():
            return str(element).strip()

    return None


def save_skipped_pmids(skipped: list[dict], output_path: str) -> None:
    """Save skipped PMIDs and reasons to a text file.

    Args:
        skipped: List of dicts with keys like:
            - pmid (str)
            - reason (str)
            - cohort (str, optional)
            - pub_types (list[str], optional)
        output_path: Where to write the .txt file.
    """
    if not skipped:
        # Still write a small file for reproducibility
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("No PMIDs were skipped.\n")
        return

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("Skipped PMIDs (and reasons)\n")
        f.write("=" * 80 + "\n")
        for item in skipped:
            pmid = str(item.get("pmid", "")).strip()
            reason = str(item.get("reason", "")).strip()
            cohort = item.get("cohort")
            pub_types = item.get("pub_types")

            line = f"PMID={pmid}"
            if cohort:
                line += f" | cohort={cohort}"
            if reason:
                line += f" | reason={reason}"
            if pub_types:
                line += f" | pub_types={'; '.join(map(str, pub_types))}"
            f.write(line + "\n")


def get_citations(pubmed_id, calls_per_second, logger=None):
    """
    Uses Entrez.elink to get citations for paper with given PubMed ID

    Parameters:
    pubmed_id         : PubMed ID of the research paper
    calls_per_second  : takes value given api_key
    logger            : optional fetch logger; request lines go to the log file instead of the console

    Returns:
    A list of PubMed IDs of citations papers
    """

    @sleep_and_retry
    @limits(calls=calls_per_second, period=1)
    def limited_get_citations():

        link_results = Entrez.elink(dbfrom="pubmed", id=pubmed_id, linkname="pubmed_pubmed_citedin")
        record = Entrez.read(link_results)
        link_results.close()

        if logger:
            logger.info(f"Getting citations for paper with PubMed ID: {int(float(pubmed_id))}")
        else:
            print(f"\nGetting citations for paper with PubMed ID: {int(float(pubmed_id))}, request made at {time.ctime()} UTC")
            print("===========================================================================================")

        try:
            # Extract the PubMed IDs of the citations
            citations = record[0].get('LinkSetDb', [])[0].get('Link', [])
            citations = [link.get('Id') for link in citations]
        except Exception:
            citations = []

        return citations

    return limited_get_citations()


def fetch_info(pubmed_id, calls_per_second, logger=None):
    """
    Uses Entrez.efetch to get info for paper with given PubMed ID

    Parameters:
    pubmed_id         : PubMed ID of the research paper
    calls_per_second  : takes value given api_key
    logger            : optional fetch logger; request lines go to the log file instead of the console

    Returns:
    records - a complex nested dictionary with paper info
    """


    @sleep_and_retry
    @limits(calls=calls_per_second, period=1)
    def limited_fetch_info():
        try:
            handle = Entrez.efetch(db="pubmed", id=pubmed_id, retmode="xml")
            records = Entrez.read(handle)
            handle.close()
            if logger:
                logger.info(f"Getting Info for paper with PubMed ID: {pubmed_id}")
            else:
                print(f"Getting Info for paper with PubMed ID: {pubmed_id}, request made at {time.ctime()} UTC")
            return records
        except Exception as e:
            if logger:
                logger.info(f"[ERROR] PubMed ID {pubmed_id}: {type(e).__name__} - {e}")
            else:
                print(f"[ERROR] PubMed ID {pubmed_id}: {type(e).__name__} - {e}")
            return None

    return limited_fetch_info()



def clean_abstract(text):
    """Remove HTML-like tags and problematic symbols from abstract text.
    Removes special symbols"""
    if not isinstance(text, str):
        return ""

    # Removing HTML/XML tags: anything enclosed in < >
    text = re.sub(r"<[^>]+>", "", text)

    # Removing trademark and service-mark symbols using Unicode escapes
    # \u00AE = ®, \u2120 = ^SM, \u2122 = ?
    text = text.replace("\u00AE", "")   # registered trademark
    text = text.replace("\u2120", "")   # service mark
    text = text.replace("\u2122", "")   # trademark

    # Normalizing unicode ? closest ASCII
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()

    # Removing non-alphanumeric except basic punctuation
    text = re.sub(r"[^a-zA-Z0-9\s\.,;:\(\)%\-]", " ", text)

    # Replacing multiple spaces created after deletion
    text = re.sub(r"\s+", " ", text).strip()

    return text


# -------------------------------------------------------------------------
# Shared helpers for both fetch modes (cohort Excel and PMID list)
# -------------------------------------------------------------------------

REVIEW_TYPES = ('Review', 'Systematic Review', 'Scoping Review')


ENTREZ_TOOL = "meshlytics"   # tool name sent to NCBI E-utilities with every request, as NCBI asks


def setup_entrez(email, api_key):
    """Sets Entrez tool/email/API key and returns the allowed calls per second (10 with API key, else 3)."""
    Entrez.tool = ENTREZ_TOOL
    if email:
        Entrez.email = email
    if api_key:
        Entrez.api_key = api_key
        calls_per_second = 10  # don't increase it to more than 10 (Max allowed limit by NCBI)
        print(f"\n[INFO] API key provided: max PubMed calls per sec = {calls_per_second}")
    else:
        calls_per_second = 3   # max allowed without API key
        print(f"\n[INFO] API key not provided: max PubMed calls per sec = {calls_per_second} (add API key for 10 per sec)")
    return calls_per_second


def extract_article_fields(article):
    """
    Extracts the output fields of one PubMed record (same fields for cohort and PMID-list fetch).

    Returns:
        dict of output fields, plus '_needs_mod' (no MeSH in PubMed but an abstract is available)
    """
    pub_types = get_publication_types(article)
    review_paper = 'Yes' if any(t in pub_types for t in REVIEW_TYPES) else 'No'
    article_type = choose_primary_publication_type(pub_types)

    citation = article.get('MedlineCitation', {})
    art = citation.get('Article', {})

    title = art.get('ArticleTitle', {})
    doi = get_doi(article)

    # Author-institution pairs: (Name||Affil1|Affil2)
    author_institution_pairs = []
    for author in art.get('AuthorList', []):
        name = f"{author.get('LastName', '')} {author.get('Initials', '')}".strip()
        affil_info = author.get("AffiliationInfo", [])
        institutions = '|'.join([a.get("Affiliation", "") for a in affil_info]) if affil_info else ""
        author_institution_pairs.append(f"({name}||{institutions})")
    author_institution_str = ','.join(author_institution_pairs)

    journal = art.get('Journal', {})
    journal_issue = journal.get('JournalIssue', {})
    year = get_publication_year(article)
    journal_name = journal.get('Title')
    journal_info = 'ISSN:' + str(journal.get('ISSN')) + ', Volume:' + str(journal_issue.get('Volume')) + ', Issue: ' + str(journal_issue.get('Issue'))
    pages = art.get('Pagination', {}).get('MedlinePgn')

    abstract_parts = art.get('Abstract', {}).get('AbstractText', [])
    abstract = clean_abstract(' '.join([str(part) for part in abstract_parts]))

    mesh_terms_list = citation.get('MeshHeadingList', [])
    mesh_tuples = [(str(item['DescriptorName']), item['DescriptorName'].attributes['UI']) for item in mesh_terms_list]
    if mesh_tuples:
        mesh_term_count = len(mesh_tuples)
        mesh_terms = ','.join([f"('{text}','{ui}')" for text, ui in mesh_tuples])
    else:
        mesh_term_count, mesh_terms = 0, None

    return {
        'review_paper': review_paper,
        'article_type': article_type,
        'title': str(title) if title else None,
        'doi': doi if doi else None,
        'author_institution': author_institution_str if author_institution_str else None,
        'year': year if year else None,
        'journal_name': journal_name if journal_name else None,
        'journal_info': journal_info if journal_info else None,
        'pages': pages if pages else None,
        'abstract': abstract if abstract else None,
        'mesh_term_count': mesh_term_count,
        'mesh_terms': mesh_terms,
        '_needs_mod': (not mesh_tuples) and bool(abstract),
    }


def run_mod_stage(rows, mesh_dictionary_path, logger, desc="MeSH on Demand", enabled=False, contact_email=None):
    """
    Optional MeSH on Demand stage (experimental, off by default; fetch --mesh-on-demand).
    Papers that have no MeSH terms in PubMed yet (not indexed) but have an abstract get MeSH terms
    suggested by NLM MeSH on Demand. Runs after all PubMed records are fetched, with its own progress bar.
    When disabled, such papers are kept without MeSH terms and listed in the summary and log.

    Returns:
        (n_requested, n_retrieved, PMIDs that need manual MeSH entry, PMIDs left without MeSH terms because MOD is off)
    """
    pending = [r for r in rows if r.get('_needs_mod')]
    if not pending:
        return 0, 0, [], []
    if not enabled:
        skipped = [str(r['pubmed_id']) for r in pending]
        for r in pending:
            r['mesh_term_count'], r['mesh_terms'] = 0, None
        tqdm.write(f"[INFO] {len(pending)} papers have no MeSH terms in PubMed yet (not indexed); they are kept without "
                   f"MeSH terms. Add --mesh-on-demand to request suggested terms from NLM MeSH on Demand (experimental).")
        logger.info(f"No MeSH terms in PubMed, MeSH on Demand off: {', '.join(skipped)}")
        return 0, 0, [], skipped

    est_min = len(pending) * 35 / 60
    tqdm.write(f"[INFO] {len(pending)} papers have no MeSH terms in PubMed; getting them from MeSH on Demand "
               f"(about 30-60 s per paper, roughly {est_min:.0f} min)")

    retrieved, manual = 0, []
    bar = tqdm(total=len(pending), desc=desc, unit="paper", dynamic_ncols=True, mininterval=0.5)
    for row in pending:
        pmid = row['pubmed_id']
        logger.info(f"Getting mesh terms using MOD API for the citation {pmid}")
        terms = mesh_on_demand(row['abstract'], log_func=lambda msg, pmid=pmid: logger.info(f"MOD {pmid}: {msg}"),
                               contact_email=contact_email)
        if terms == 'manual_check':
            logger.info(f"citation with pubmed_id: {pmid} needs manual entry")
            manual.append(pmid)
            row['mesh_term_count'], row['mesh_terms'] = 0, None
        else:
            mesh_term_count, mesh_terms = get_mesh_tuples_from_terms(terms, mesh_dictionary_path)
            row['mesh_term_count'] = mesh_term_count if mesh_term_count else 0
            row['mesh_terms'] = mesh_terms if mesh_terms else None
            retrieved += 1
            logger.info(f"MOD {pmid}: {row['mesh_term_count']} MeSH terms")
        bar.update(1)
        bar.set_postfix(retrieved=retrieved, manual_check=len(manual))
    bar.close()
    return len(pending), retrieved, manual, []


def format_duration(seconds):
    """Formats seconds as '1 h 02 min', '4 min 12 s' or '41 s'."""
    seconds = int(round(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} h {(seconds % 3600) // 60:02d} min"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds} s"


def print_fetch_summary(stats, calls_per_second, log_path, manual_pmids, no_mesh_pmids=()):
    """Prints the end-of-fetch summary (PubMed speed, exclusions, MeSH on Demand results, log path)."""
    rate = stats['pubmed_requests'] / stats['pubmed_seconds'] if stats['pubmed_seconds'] > 0 else 0
    print(f"\n[SUMMARY] PubMed: {stats['pubmed_requests']} papers requested in {format_duration(stats['pubmed_seconds'])} "
          f"({rate:.1f} papers/s, limit {calls_per_second}/s) | kept: {stats['kept']} | "
          f"excluded by publication type: {stats['excluded_type']} | errors: {stats['errors']}")
    if stats['mod_requested']:
        print(f"[SUMMARY] MeSH on Demand: {stats['mod_requested']} papers, {stats['mod_retrieved']} retrieved, "
              f"{len(manual_pmids)} need manual MeSH entry | {format_duration(stats['mod_seconds'])}")
        if manual_pmids:
            shown = ', '.join(manual_pmids[:10]) + (' ...' if len(manual_pmids) > 10 else '')
            print(f"[SUMMARY] Manual MeSH entry needed for PMIDs: {shown}")
    if no_mesh_pmids:
        shown = ', '.join(no_mesh_pmids[:10]) + (' ...' if len(no_mesh_pmids) > 10 else '')
        print(f"[SUMMARY] Without MeSH terms (not yet indexed in PubMed; MeSH on Demand off): {len(no_mesh_pmids)} papers: {shown}")
    print(f"[INFO] Detailed request log (timestamps per paper): {log_path}")


def new_fetch_stats():
    return {'pubmed_requests': 0, 'pubmed_seconds': 0.0, 'kept': 0, 'excluded_type': 0, 'errors': 0,
            'mod_requested': 0, 'mod_retrieved': 0, 'mod_seconds': 0.0}


def fetch_pubmed_rows(plan, calls_per_second, logger, stats, skipped_pmids, desc, cohort=None):
    """
    PubMed stage: fetches and screens the records in `plan` (list of (cohort_paper_pmid or None, pmid)),
    showing a progress bar with papers/s, papers lacking MeSH and skipped papers.

    Returns:
        list of row dicts in plan order (MeSH on Demand not yet applied)
    """
    rows = []
    no_mesh = 0
    skipped_before = stats['excluded_type'] + stats['errors']  # counters on this bar are for this stage only
    bar = tqdm(total=len(plan), desc=desc, unit="paper", dynamic_ncols=True, mininterval=0.5)
    t0 = time.time()
    for cohort_paper_pmid, pubmed_id in plan:
        skip_entry = {"pmid": str(pubmed_id)}
        if cohort is not None:
            skip_entry["cohort"] = str(cohort)
        try:
            stats['pubmed_requests'] += 1
            records = fetch_info(pubmed_id, calls_per_second, logger=logger)
            if records is None:
                skipped_pmids.append({**skip_entry, "reason": "Malformed/empty efetch result (records is None)"})
                stats['errors'] += 1
                continue
            if not records.get('PubmedArticle'):
                skipped_pmids.append({**skip_entry, "reason": "No PubmedArticle in efetch result (e.g. book record)"})
                stats['errors'] += 1
                continue

            article = records['PubmedArticle'][0]

            # Exclude non-original / commentary / erratum-like records
            pub_types = get_publication_types(article)
            exclude_rec, exclude_hits = should_exclude_by_publication_type(pub_types)
            if exclude_rec:
                skipped_pmids.append({**skip_entry, "reason": "Excluded by PublicationTypeList", "pub_types": pub_types, "matched": exclude_hits})
                logger.info(f"[INFO] Skipping PMID {pubmed_id} due to PublicationTypeList exclusion: {exclude_hits}")
                stats['excluded_type'] += 1
                continue

            fields = extract_article_fields(article)
            row = {'pubmed_id': pubmed_id}
            if cohort_paper_pmid is not None:
                row['associated_cohort_paper_id'] = cohort_paper_pmid
            row.update(fields)
            rows.append(row)
            stats['kept'] += 1
            if fields['_needs_mod']:
                no_mesh += 1

        except Exception as e:
            skipped_pmids.append({**skip_entry, "reason": f"Exception during processing: {e}"})
            logger.info(f"[ERROR] Error processing {pubmed_id}: {type(e).__name__} - {e}")
            stats['errors'] += 1
        finally:
            bar.update(1)
            bar.set_postfix(no_mesh=no_mesh, skipped=stats['excluded_type'] + stats['errors'] - skipped_before)
    bar.close()
    stats['pubmed_seconds'] += time.time() - t0
    return rows


def rows_to_dataframe(rows):
    """Row dicts -> DataFrame in the original column order (helper flags removed)."""
    df = pd.DataFrame(rows)
    return df.drop(columns=[c for c in df.columns if c.startswith('_')])


def fetch_data(excel_file_path, email, api_key, mesh_dictionary_path, use_mesh_on_demand=False):
    """
    Gets the data from Entrez and adds it to the EXCEL file

    Parameters:
    email               : Email ID of user
    api_key             : API Key obtained from NCBI website
    excel_file_path     : Path tp the EXCEL File with Cohort PubMed IDs
    mesh_dictionary_path: Path to MeSH Dictionary

    Stages per cohort:
      1. citing papers of each cohort paper (Entrez elink); a citing paper is kept only once,
         under the first cohort / cohort paper that returns it
      2. PubMed records of the citing papers (progress bar: papers/s, missing MeSH, skipped)
      3. MeSH on Demand for papers without MeSH terms (own progress bar)
      4. cohort sheet appended to the Excel file
    Per-request lines with timestamps are written to <excel name>_fetch_log.txt.
    """
    calls_per_second = setup_entrez(email, api_key)

    # Validate file
    if not os.path.exists(excel_file_path):
        raise FileNotFoundError(f"Input file not found: {excel_file_path}")
    if not excel_file_path.lower().endswith(".xlsx"):
        raise ValueError("Input file must be a .xlsx file with required format for Cohort Ids")

    # Creating a dictionary from Excel Sheet to loop over PMIDs
    df = pd.read_excel(excel_file_path) #, sheet_name='Cohorts'

    if 'cohort name' in df.columns:
      df = df.drop(columns=['cohort name'])

    dict_cohort_PMIDs = {}
    for i in range(len(df)):
      cohort_PMIDs = [x for x in df.iloc[i].to_list() if str(x) != 'nan']
      cohort = cohort_PMIDs[0]
      PMIDs = [str(x) for x in cohort_PMIDs[1:]]
      if len(PMIDs) == 0:
        continue
      dict_cohort_PMIDs[cohort] = PMIDs

    log_path = os.path.splitext(excel_file_path)[0] + "_fetch_log.txt"
    logger = get_fetch_logger(log_path)
    logger.info(f"=== fetch (cohort Excel): {os.path.abspath(excel_file_path)} | {len(dict_cohort_PMIDs)} cohorts | max calls/s {calls_per_second}")

    processed_unique_citations_for_cohort = set()  # shared across cohorts (a citing paper is stored once)
    skipped_pmids = []  # list of dicts: {pmid, cohort, reason, pub_types}
    stats = new_fetch_stats()
    manual_pmids = []
    no_mesh_pmids = []

    try:
        for c_index, (cohort, cohort_PMIDs) in enumerate(dict_cohort_PMIDs.items(), start=1):

            # --- Stage 1: citing papers of the cohort papers ---
            plan = []  # (cohort paper PMID, citing paper PMID)
            already_included = 0
            for PMID in cohort_PMIDs:
                citations = get_citations(PMID, calls_per_second, logger=logger)  # PMID should be a string not int
                for pubmed_id in citations:
                    # Skipping the citation if it is already included via another cohort paper (any cohort)
                    # Mainly to reduce the no. of API requests
                    if pubmed_id in processed_unique_citations_for_cohort:
                        already_included += 1
                        continue
                    processed_unique_citations_for_cohort.add(pubmed_id)
                    plan.append((PMID, pubmed_id))

            print(f"\n[INFO] Cohort {c_index}/{len(dict_cohort_PMIDs)} '{cohort}' | cohort papers: {len(cohort_PMIDs)} | "
                  f"citing papers to fetch: {len(plan)} | already included earlier: {already_included}")
            if not plan:
                print(f"Skipping cohort with no citations: {cohort}")
                continue

            # --- Stage 2: PubMed records ---
            rows = fetch_pubmed_rows(plan, calls_per_second, logger, stats, skipped_pmids,
                                     desc=f"{cohort} | PubMed", cohort=cohort)

            # --- Stage 3: MeSH on Demand for papers without MeSH terms ---
            t0 = time.time()
            n_req, n_ok, manual, no_mesh = run_mod_stage(rows, mesh_dictionary_path, logger, desc=f"{cohort} | MeSH on Demand",
                                                         enabled=use_mesh_on_demand, contact_email=email)
            stats['mod_requested'] += n_req
            stats['mod_retrieved'] += n_ok
            stats['mod_seconds'] += time.time() - t0
            manual_pmids.extend(manual)
            no_mesh_pmids.extend(no_mesh)

            # --- Stage 4: cohort sheet ---
            combined_df = rows_to_dataframe(rows)
            if combined_df.empty:
                print(f"Skipping cohort with no citations: {cohort}")
                continue
            with pd.ExcelWriter(excel_file_path, engine='openpyxl', mode='a') as writer:
                combined_df.to_excel(writer, sheet_name = cohort , index=False)
            print(f"[INFO] Cohort '{cohort}': {len(combined_df)} papers saved to sheet '{cohort}'")

        print_fetch_summary(stats, calls_per_second, log_path, manual_pmids, no_mesh_pmids)
        logger.info(f"=== done: {stats}")
    finally:
        close_fetch_logger(logger)

    # Save skipped PMIDs log (same folder as the cohort Excel)
    skipped_file = os.path.splitext(excel_file_path)[0] + "_skipped_pmids.txt"
    save_skipped_pmids(skipped_pmids, skipped_file)
    print(f"[INFO] Skipped PMIDs log saved to: {skipped_file}")

    generate_mesh_terms_with_keys_and_counts(excel_file_path, mesh_dictionary_path)

    update_cohort_citation_counts(excel_file_path)

    print('\nThe Excel file was successfully updated with fetched data located at: ')
    print(f'{excel_file_path}')
    print('An excel file with additional mesh term data with Path with Keys and counts is saved at the same location.')



def fetch_data_from_txt(txt_file_path, email, api_key, mesh_dictionary_path, use_mesh_on_demand=False):
    """
    Accepts a plain text file with a list of PubMed IDs and fetches metadata for each.
    Outputs an Excel file with results.

    Parameters:
    - txt_file_path : path to .txt file with PubMed IDs
    - email         : user email (required by Entrez)
    - api_key       : NCBI API key
    - mesh_dictionary_path : path to MeSH dictionary

    Output:
    - An Excel file with citation data for given PubMed IDs (<txt name>_fetched_data.xlsx)
    - <txt name>_skipped_pmids.txt and <txt name>_fetch_log.txt (timestamps per paper)
    """
    calls_per_second = setup_entrez(email, api_key)

    # Validate file
    if not os.path.exists(txt_file_path):
        raise FileNotFoundError(f"Input file not found: {txt_file_path}")
    if not txt_file_path.lower().endswith(".txt"):
        raise ValueError("Input file must be a .txt file with PubMed IDs")

    # Read and sanitize the PubMed IDs
    with open(txt_file_path, 'r') as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        raise ValueError("Text file is empty or improperly formatted.")

    # Check if first line is a title (non-numeric)
    if not lines[0].isdigit():
        print(f"Detected header: '{lines[0]}', ignoring first line.")
        lines = lines[1:]

    pubmed_ids = [line for line in lines if line.isdigit()]

    if not pubmed_ids:
        raise ValueError("No valid PubMed IDs found in the file.")

    # Unique PMIDs, input order kept
    plan = [(None, pmid) for pmid in dict.fromkeys(pubmed_ids)]

    log_path = os.path.splitext(txt_file_path)[0] + "_fetch_log.txt"
    logger = get_fetch_logger(log_path)
    logger.info(f"=== fetch (PMID list): {os.path.abspath(txt_file_path)} | {len(plan)} PMIDs | max calls/s {calls_per_second}")
    print(f"[INFO] {len(plan)} unique PubMed IDs to fetch")

    skipped_pmids = []  # list of dicts: {pmid, reason, pub_types}
    stats = new_fetch_stats()
    try:
        # --- Stage 1: PubMed records ---
        rows = fetch_pubmed_rows(plan, calls_per_second, logger, stats, skipped_pmids, desc="PubMed")

        # --- Stage 2: MeSH on Demand for papers without MeSH terms ---
        t0 = time.time()
        n_req, n_ok, manual_pmids, no_mesh_pmids = run_mod_stage(rows, mesh_dictionary_path, logger,
                                                                 enabled=use_mesh_on_demand, contact_email=email)
        stats['mod_requested'], stats['mod_retrieved'], stats['mod_seconds'] = n_req, n_ok, time.time() - t0

        print_fetch_summary(stats, calls_per_second, log_path, manual_pmids, no_mesh_pmids)
        logger.info(f"=== done: {stats}")
    finally:
        close_fetch_logger(logger)

    dataframe = rows_to_dataframe(rows)

    # Save output Excel file
    output_file = os.path.splitext(txt_file_path)[0] + "_fetched_data.xlsx"
    dataframe.to_excel(output_file, index=False)
    # Save skipped PMIDs log (same folder as output)
    skipped_file = os.path.splitext(txt_file_path)[0] + "_skipped_pmids.txt"
    save_skipped_pmids(skipped_pmids, skipped_file)
    print(f"[INFO] Skipped PMIDs log saved to: {skipped_file}")
    print(f"\nFetched citation data saved to: {output_file}")

    generate_mesh_terms_with_keys_and_counts(output_file, mesh_dictionary_path)
    print('An excel file with additional mesh term data with Path with Keys and counts is saved at the same location.')



def generate_mesh_terms_with_keys_and_counts(excel_file_path, mesh_dictionary_path):
    """
    Processes citation data Excel file to generate MeSH term hierarchy with counts.
    Handles both:
    - Excel from cohort-based fetching (multiple sheets, skips first)
    - Excel from .txt-based fetching (one sheet)
    """

    # Uses the MeSH dictionary passed by the caller (cli.py -> MESH_DICTIONARY_FILE),
    # so mesh-count and fetch use the same MeSH version as the rest of the pipeline
    data_dir = os.path.dirname(mesh_dictionary_path)
    keys_and_classes_path = os.path.join(data_dir, 'keys_and_classes.json')
    excel_file_dir = os.path.dirname(os.path.abspath(excel_file_path))

    xls = pd.ExcelFile(excel_file_path)
    sheet_names = xls.sheet_names

    # Process logic: skip first sheet only if there are multiple
    if len(sheet_names) == 1:
        relevant_sheets = sheet_names
    else:
        relevant_sheets = sheet_names[1:]

    dfs = []
    for sheet in relevant_sheets:
        df_sheet = pd.read_excel(xls, sheet_name=sheet)
        if 'review_paper' in df_sheet.columns:
            df_sheet = df_sheet[df_sheet['review_paper'] == 'No']
            # If newer fetch added article_type, also exclude non-original publication types for safety
            if 'article_type' in df_sheet.columns:
                df_sheet = df_sheet[~df_sheet['article_type'].isin(EXCLUDE_PUBLICATION_TYPES)]
            dfs.append(df_sheet)
        else:
            print(f"Warning: Sheet '{sheet}' does not contain 'review_paper' column. Skipping.")

    if not dfs:
        print("No valid non-review data found to analyze.")
        return

    df = pd.concat(dfs, ignore_index=True)

    # Load supporting dictionaries
    with open(keys_and_classes_path, 'r') as file:
        keys_and_classes = json.load(file)

    # every tree position of every descriptor (a term in several branches lists all its paths, ' | ' separated)
    from meshlytics.utils import load_mesh_index, format_mesh_path
    mesh_index = load_mesh_index(mesh_dictionary_path)

    def extract_term_uids(mesh_terms_str):
        if pd.isna(mesh_terms_str):
            return []
        return re.findall(r"\('([^']*)','(D\d+)'\)", str(mesh_terms_str))

    term_counter = Counter()
    term_of_uid = {}
    for pairs in df['mesh_terms'].apply(extract_term_uids):
        for term, uid in pairs:
            term_counter[uid] += 1
            term_of_uid.setdefault(uid, term)

    rows = []
    for uid, count in term_counter.items():
        entry = mesh_index.get(uid)
        term = term_of_uid[uid]   # name as stored in the records, so filter-data matches it even if NLM renamed it
        if entry:
            paths = ' | '.join(format_mesh_path(p, keys_and_classes) for p in entry['paths'])
            n_paths = len(entry['paths'])
        else:
            # e.g. the check tags Male / Female, which have no tree number; listed so they can be marked too
            paths, n_paths = 'No tree position (check tag or descriptor not in this MeSH version)', 0
        rows.append({'Term': term, 'UID': uid, 'Path with Keys': paths, 'Tree positions': n_paths, 'Count': count})

    terms_df_full = pd.DataFrame(rows, columns=['Term', 'UID', 'Path with Keys', 'Tree positions', 'Count']).sort_values(by='Count', ascending=False)

    terms_df_full['filter_out_term?'] = 'N'

    output_path = get_safe_keyword_file_path(excel_file_dir)  # never overwrites a keyword file with marked terms
    terms_df_full.to_excel(output_path, index=False)
    print(f"PubMed Citation data saved to: {output_path}")
