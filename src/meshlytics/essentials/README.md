# essentials: reference data shipped with Meshlytics

## MeSH data (last updated: 9 November 2026)

| Item | Value |
|---|---|
| MeSH version in use | **MeSH 2026** (`MeSH_Dictionary_2026.json`, set in `src/meshlytics/cli.py` as `MESH_DICTIONARY_FILE`) |
| Source file | `desc2026.xml`, NLM MeSH descriptor XML, https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/desc2026.xml |
| Source file date (server Last-Modified) | 12 Aug 2026 |
| Downloaded | 9 Nov 2026 (313 MB; SHA-256 449792ed59ba4d60035e44724352f7361ec0dc945b67dcd821a50ff32f81bcdb) |
| Newer version available? | Check the folder above for descYYYY.xml with a later year (NLM releases the next year's MeSH near the end of each year). |
| Descriptors in the XML | 31,110 (2 without a tree number: the check tags Male and Female) |
| Descriptors in the dictionary | 31,108 |
| Descriptors with more than one level-3 position | 13,644 (43.9%); in more than one top-level category: 2,657 (8.5%) |
| Built with | `scripts/build_mesh_dictionary.py` |

The XML itself is not kept here because of its size. To rebuild or update:

```
python scripts/build_mesh_dictionary.py <path to descYYYY.xml>
```
Then set `MESH_DICTIONARY_FILE` in `src/meshlytics/cli.py` to the new file and update the table above.

### What changed with the 2026 build (compared with MeSH_Dictionary_2025.json)
* Built from MeSH 2026: 270 descriptors added, 6 removed, 92 with changed tree positions, 26 renamed category labels
  (e.g. Firmicutes -> Bacillota, Health Care Facilities, Manpower, and Services -> Health Care Facilities Workforce and Services).
* Descriptors whose own tree number is a level-2 code (115 tree numbers, e.g. Neoplasms C04, Mental Disorders F03,
  Nervous System A08) were skipped by the earlier builder. They are now included under a level-3 key equal to their
  level-2 code, e.g. `{"C": {"C04": {"C04": {"D009369": "Neoplasms"}}}}`. Meshlytics reads such a path as "no level 3".
* `keys_and_classes.json` (labels of the level-1/2/3 codes) is regenerated from the same XML; the previous file is kept as
  `keys_and_classes_before_2026.json`.
* Older dictionaries (`MeSH_Dictionary_2024.json`, `MeSH_Dictionary_2025.json`) are kept for reproducing earlier runs.

### How Meshlytics uses every tree position (code change of 9 Nov 2026)
* Keyword table (`mesh-count`, `fetch`): lists all tree paths of a term, ` | ` separated, plus columns `UID` and
  `Tree positions`. Terms without a tree position (Male, Female) are listed too, so they can be marked for removal.
* `filter-data --exclude-categories`: by default (`--category-match all`) a term is removed only if all its paths are in
  excluded branches (e.g. Parents, in F01, I01 and M01, is kept when M is excluded); `--category-match any` removes it if
  any path is.
* Level 1-3 co-occurrence matrices: every tree position of both descriptors is used; a descriptor pair adds 1 to each
  distinct category pair it links (pairs within one level-1 category are not counted, as before).
* `analysis_summary.txt` records the MeSH dictionary used.

## Other files
* `pubmed_journals_11062026.json`: 38,106 NLM journal records (journal matching for `top-cited`), downloaded 6 Nov 2026.
