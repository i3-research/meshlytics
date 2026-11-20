# -*- coding: utf-8 -*-
"""
institution_processing.py

Script to standardize and shorten institution names in author_institution column
using GPT and update the Excel file with a new column author_institution_llm.
"""

import json
import pandas as pd
from openai import OpenAI
from pathlib import Path

import logging
for name in ("httpx", "httpcore", "openai"):
    logging.getLogger(name).setLevel(logging.WARNING)


# Helper function

def derive_output_path(file_path: str) -> Path:
    """
    Create output path in the same directory as input file,
    inserting '_with_llm_standard_institution_names' before extension.
    """
    input_path = Path(file_path)

    if input_path.suffix.lower() != ".xlsx":
        raise ValueError("Institution processing currently supports only .xlsx files")

    return input_path.with_name(
        f"{input_path.stem}_with_llm_standard_institution_names{input_path.suffix}"
    )


# ---------------------------
# Step 1 Preprocess entries
# ---------------------------
def preprocess_author_institution(df, author_col="author_institution"):
    """
    Extracts authors + first institutions from author_institution column using
    manual parenthesis depth tracking.
    """
    updated_entries = []
    inst_list = set()

    for entry in df[author_col]:
        if pd.isna(entry):
            updated_entries.append("")   # keep alignment
            continue

        new_tuples, buffer, depth, tuples = [], [], 0, []

        for ch in entry:
            if ch == "(":
                if depth == 0:
                    buffer = []
                depth += 1
                buffer.append(ch)
            elif ch == ")":
                buffer.append(ch)
                depth -= 1
                if depth == 0:
                    tuples.append("".join(buffer))
            else:
                if depth > 0:
                    buffer.append(ch)

        for tup in tuples:
            if "||" in tup:
                try:
                    inside = tup.strip("()")
                    author, inst = inside.split("||", 1)
                    first_inst = inst.split("|")[0].strip(" ,")
                    if first_inst:
                        inst_list.add(first_inst)
                        new_tuples.append(f"({author.strip()}||{first_inst})")
                except Exception as e:
                    print(f"[WARN] Failed to parse tuple: {tup} ({e})")
                    continue

        updated_entries.append(",".join(new_tuples))

    df["author_institution_clean"] = updated_entries
    return df, list(inst_list)


# -----------------------------------------
# Step 2 Batch to GPT for normalization
# -----------------------------------------
def batch_standardize_institutions(inst_list, client, batch_size=30, model="gpt-4o"):
    """
    Sends institutions in batches to GPT-4o for canonical + short name mapping.
    Aligns results by index instead of asking GPT to return 'original'.
    Returns dictionary: {original: (standardized, short)}.
    """
    results = {}

    for i in range(0, len(inst_list), batch_size):
        batch = inst_list[i:i+batch_size]

        print(f"[INFO] Processing batch {i//batch_size+1} "
              f"({i+1}-{i+len(batch)} of {len(inst_list)})")

        # Format batch as clean numbered list
        batch_str = "\n".join([f"{j+1}. {inst}" for j, inst in enumerate(batch)])

        prompt = f"""
        Task: Extract the main institution name from an author affiliation string and provide both a standardized full name and a consistent short name.

        Rules:

        1. Parent-University Mapping:
          - Always map sub-centres, hospitals, schools, and research institutes to their parent university if they factually belong to one (even if the parent is not explicitly named in the text).
            - "Harvard Medical School" ? "Harvard University"
            - "Stanford School of Medicine" ? "Stanford University"
            - "McGill University Health Centre", "RI-MUHC" ? "McGill University"
            - "Universite de Sherbrooke", "CHUS" ? "Universite de Sherbrooke"

          - Special Sub-rule: If the name contains "Medical Center (MC)", "University Hospital", "University Medical Center (UMC)", or equivalent,
            and it is factually affiliated with a university, always collapse it to the parent university.
            - "Erasmus MC" ? "Erasmus University" (short: Erasmus U)
            - "Leiden University Medical Center" ? "Leiden University" (short: Leiden U)
            - "UMC Utrecht" ? "Utrecht University" (short: Utrecht U)

        2. Standalone Institutions:
          - If the institution is standalone and not a university (e.g., Sunnybrook Health Sciences Centre, SickKids, NRC), keep it as is.

        3. Multi-Campus Universities:
          - Treat multi-campus systems as distinct universities, never collapse them into a single "University of California".
          - Use the standard campus-level naming convention.
          - Rules for short forms:
            - If the campus has a globally recognized acronym, always keep the acronym:
              - "University of California, San Francisco" ? "UCSF"
              - "University of California, Los Angeles" ? "UCLA"
              - "University of Southern California" ? "USC"
              - "University of Texas Southwestern Medical Center" ? "UTSW"
              - "State University of New York at Albany" ? "SUNY Albany"
              - "City University of New York, Hunter College" ? "CUNY Hunter"
              - "University of North Carolina at Chapel Hill" ? "UNC Chapel Hill"
              - etc.
            - If the campus does not have a famous acronym, keep the "UC [Campus]" (or system acronym + campus) form:
              - "University of California, Berkeley" ? "UC Berkeley"
              - "University of California, San Diego" ? "UC San Diego"
              - "University of California, Davis" ? "UC Davis"
              - "University of California, Irvine" ? "UC Irvine"
              - "University of California, Santa Cruz" ? "UC Santa Cruz"
              - "University of California, Riverside" ? "UC Riverside"
              - "University of California, Merced" ? "UC Merced"
              - etc.

        4. Institution Selection Hierarchy:
          - The hierarchy determines which institution to keep when multiple names appear in the same string. Select the institution with the highest academic/general scope unless the lower-level unit is truly standalone.
            - University / College
            - Hospital / Medical Center
            - Research Institute / Center / Academy / School of Medicine
            - Clinic
            - Health Board / Health System / Trust
            - Government Institute / National Organization
            - Others (nonprofits, companies, consortia, etc.)
          - Sub-rule: If a hospital, medical center, or research institute is explicitly affiliated with or named after a university, collapse it to the parent university.
            - "Harbor-UCLA Medical Center" ? "University of California, Los Angeles" (short: UCLA)
            - "Stanford University School of Medicine" ? "Stanford University" (short: Stanford)
          - Exception: Famous standalone medical institutions must remain standalone (e.g., Mayo Clinic, Cleveland Clinic, St. Jude Children�s Research Hospital).

        5. Standardized Name:
          - Return the canonical, human-readable institution name.
          - Remove departments, units, street addresses, and emails.
          - Examples:
            - "Department of Paediatrics, University of Oxford, Oxford, UK" ? "University of Oxford"
            - "Child and Adolescent Psychiatry, St. David�s Hospital Cardiff, Cwm Taf Health Board, Wales" ? "St. David�s Hospital Cardiff"

        6. Short Name (shortest possible, =15 characters, and always consistent):

          a. General Principles:
            - Use the shortest, widely recognized acronym or single name.
            - If a well-known acronym exists (e.g., KKH for KK Women�s and Children�s Hospital), always use the acronym instead of truncating (avoid "KK Hospital").
            - Use "U" for "University" where appropriate (e.g., "McGill U", "Toronto U").
            - Hospitals/research centres: Use acronyms (e.g., "CHUSJ", "SickKids").
            - Acronyms must be uppercase.
            - Never truncate famous names:
              - "Johns Hopkins University" ? "Johns Hopkins" (not "Johns Hpk")
            - Keep case style consistent: acronyms uppercase; words in title case.

          b. Famous Universities � Single-Word:
            - Harvard, Yale, Brown, Princeton, Oxford, Stanford, Columbia, Dartmouth, Cornell, Cambridge, Duke

          c. Famous Universities � Acronyms:
            - MIT, Caltech, UPenn, UCLA, UCSF, USC, KCL, UCL, NYU,
              ETH Zurich, NUS, NTU, UIUC, UMich, UTSW, CUHK, HKU, ANU, UNSW

          d. Disambiguation Rule (for ambiguous acronyms):
            - If an acronym is not globally recognized and could repeat across institutions, expand with the leading unique word + "U" or "MU".
              - "Khyber Medical University" ? "Khyber MU" (not KMU)
              - "Kaohsiung Medical University" ? "Kaohsiung MU" (not KMU)

          e. Consistency:
            - The same institution must always resolve to the same short form across all queries and batches.
            - Multi-language consistency: different language variants of the same institution must collapse to one short name.
              - "Universit� de Montr�al", "University of Montreal" ? "UdeM"

        7. Networks, Consortia, and Agencies:
          - Generate acronyms from initials when appropriate.
            - "Canada Fetal Alcohol Spectrum Disorder Research Network" ? "CFASDRN"
            - "Canadian Institutes of Health Research" ? "CIHR"

        8. Never Guess:
          - Do not substitute an institution that is not present in the text unless mapping to a parent via Rule 1.
          - If no recognizable institution (from the hierarchy) exists, return "Unknown".

        9. Companies, Industry Labs, and Private Research:
          - Standardized should be the official name.
          - Short form should be the widely recognized company acronym or short name.
            - "International Business Machines" ? "IBM"
            - "Google Research" ? "Google"
            - "Siemens Healthineers" ? "Siemens"

        10. Email Domain Fallback:
          - If the string is noisy or unclear but contains an email domain, infer the institution from it.
            - "@harvard.edu" ? "Harvard"
            - "@ox.ac.uk" ? "Oxford"


        Return ONLY valid JSON in this format (same order as input):
        [
          {{"standardized": "canonical institution", "short": "short key"}}
        ]

        Input list:
        {batch_str}
        """
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0
            )

            content = resp.choices[0].message.content.strip()

            if content.startswith("```"):
                content = content.strip("`")
                if content.lower().startswith("json"):
                    content = content[4:].strip()

            batch_json = json.loads(content)

            for orig, rec in zip(batch, batch_json, strict=False):
                results[orig.rstrip(" .").strip()] = (rec["standardized"], rec["short"])

        except Exception as e:
            print(f"[ERROR parsing GPT batch] {e}")
            if 'content' in locals():
                print("---- RAW CONTENT START ----")
                print(content[:800])
                print("---- RAW CONTENT END ----")

            # Retry with smaller batch size
            if batch_size > 10:
                print("[INFO] Retrying with smaller batch size (10)...")
                results.update(batch_standardize_institutions(batch, client=client, batch_size=10, model=model))
            continue

    return results


# --------------------------------------------------------
# Step 3 Apply mapping and build new institution column
# --------------------------------------------------------
def update_author_institution_llm(df, mapping, author_col="author_institution_clean"):
    """
    Rebuilds tuples replacing institution with shortened key.
    Falls back to acronym generation if mapping fails.
    """
    updated_entries = []

    def generate_acronym(name):
        words = [w for w in name.replace(",", "").split() if w]
        acronym = "".join([w[0].upper() for w in words])
        return acronym[:10] if acronym else "UNK"

    for entry in df[author_col]:
        if pd.isna(entry) or entry == "":
            updated_entries.append("")
            continue

        buffer, depth, tuples, new_tuples = [], 0, [], []

        for ch in entry:
            if ch == "(":
                if depth == 0:
                    buffer = []
                depth += 1
                buffer.append(ch)
            elif ch == ")":
                buffer.append(ch)
                depth -= 1
                if depth == 0:
                    tuples.append("".join(buffer))
            else:
                if depth > 0:
                    buffer.append(ch)

        for tup in tuples:
            if "||" in tup:
                try:
                    inside = tup.strip("()")
                    author, inst = inside.split("||", 1)
                    inst = inst.strip()
                    key = inst.rstrip(" .").strip()

                    std, short = mapping.get(key, (inst, "Unknown"))
                    if not short or short.lower() in ("unknown", "na"):
                        short = generate_acronym(std)

                    new_tuples.append(f"({author.strip()}||{short})")
                except Exception as e:
                    print(f"[WARN] Failed to parse tuple: {tup} ({e})")
                    continue

        updated_entries.append(",".join(new_tuples))

    df["author_institution_llm"] = updated_entries
    return df


# ---------------------------
# Step 4 Save dictionary
# ---------------------------
def save_mapping(mapping, output_path=None):
    """
    Save combined mapping dictionary to Excel.
    """
    rows = [{"Original": k, "Standardized": v[0], "Short": v[1]} for k, v in mapping.items()]
    df_map = pd.DataFrame(rows)

    if output_path is None:
        raise ValueError("save_mapping requires output_path when called.")

    df_map.to_excel(output_path, index=False)
    print(f"[INFO] Combined mapping saved to {output_path}")


# ---------------------------
# Step 5 Main driver
# ---------------------------
def main(file_path, open_ai_api_key: str, model: str = "gpt-4o"):

    if not open_ai_api_key:
        raise RuntimeError("OpenAI API key is required for institution processing.")

    client = OpenAI(api_key=open_ai_api_key)
    print(f"[INFO] OpenAI model: {model}")

    output_path = derive_output_path(file_path)
    mapping_output_path = output_path.with_name(f"{output_path.stem}_institution_mapping.xlsx")

    all_sheets = pd.read_excel(file_path, sheet_name=None)
    combined_mapping = {}
    updated_sheets = {}

    for sheet_name, df in all_sheets.items():
        if "author_institution" not in df.columns:
            updated_sheets[sheet_name] = df
            continue

        print(f"[INFO] Processing sheet: {sheet_name}")
        df, inst_list = preprocess_author_institution(df)
        print(f"[INFO] Found {len(inst_list)} unique institutions in {sheet_name}")

        mapping = batch_standardize_institutions(inst_list, client=client, model=model)
        combined_mapping.update(mapping)

        df = update_author_institution_llm(df, mapping)
        updated_sheets[sheet_name] = df

    save_mapping(combined_mapping, output_path=str(mapping_output_path))

    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        for name, df in updated_sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)

    print(f"[INFO] Updated Excel saved to {output_path}")
