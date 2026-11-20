"""
build_mesh_dictionary.py

Builds the MeSH dictionary files used by Meshlytics from an NLM MeSH descriptor XML file (descYYYY.xml).
Based on the original Colab builder (two-level keys, then nested by category), with two additions:

  1. Descriptors whose own tree number is a level-2 code (e.g. Neoplasms = C04, Mental Disorders = F03)
     were skipped by the original builder (it kept tree numbers with >= 2 parts only). They are now stored
     under a level-3 key equal to their level-2 code:  {"C": {"C04": {"C04": {"D009369": "Neoplasms"}}}}
     so every descriptor in the tree has at least one path. Meshlytics reads such a path as level 3 = none.
  2. keys_and_classes.json (labels of the level-1/2/3 codes) is regenerated from the same XML.

Output format (unchanged):  { L1: { L2: { L3: { DescriptorUI: DescriptorName } } } }
A descriptor appears under every level-3 node (or level-2 node) of all its tree numbers.

Usage (from the repository folder):
  python scripts/build_mesh_dictionary.py <path to descYYYY.xml> [--year YYYY]
Download descYYYY.xml from https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/
Writes src/meshlytics/essentials/MeSH_Dictionary_<YYYY>.json and src/meshlytics/essentials/keys_and_classes.json (the previous
keys_and_classes.json is kept as keys_and_classes_<old year or date>.json), then update MESH_DICTIONARY_FILE
in src/meshlytics/cli.py and the notes in src/meshlytics/essentials/README.md.
"""

import os
import re
import json
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime

LEVEL1_NAMES = {
    "A": "Anatomy", "B": "Organisms", "C": "Diseases", "D": "Chemicals and Drugs",
    "E": "Analytical, Diagnostic and Therapeutic Techniques, and Equipment", "F": "Psychiatry and Psychology",
    "G": "Phenomena and Processes", "H": "Disciplines and Occupations",
    "I": "Anthropology, Education, Sociology, and Social Phenomena", "J": "Technology, Industry, and Agriculture",
    "K": "Humanities", "L": "Information Science", "M": "Named Groups", "N": "Health Care",
    "V": "Publication Characteristics", "Z": "Geographicals",
}


def parse_descriptors(xml_file):
    """Streams the descriptor XML; yields (DescriptorUI, DescriptorName, [tree numbers])."""
    for _event, elem in ET.iterparse(xml_file, events=("end",)):
        if elem.tag == "DescriptorRecord":
            ui = elem.findtext("DescriptorUI")
            name = elem.findtext("DescriptorName/String")
            trees = [t.text for t in elem.findall("TreeNumberList/TreeNumber") if t.text]
            yield ui, name, trees
            elem.clear()


def build(xml_file):
    mesh, labels = {}, dict(LEVEL1_NAMES)
    n_desc = n_l2_level = n_no_tree = 0
    for ui, name, trees in parse_descriptors(xml_file):
        n_desc += 1
        if not trees:
            n_no_tree += 1          # check tags such as Male / Female have no tree number
            continue
        for tn in trees:
            parts = tn.split(".")
            l1, l2 = tn[0], parts[0]
            l3 = ".".join(parts[:2]) if len(parts) >= 2 else l2   # level-2 descriptor -> l3 key = l2 code
            if len(parts) == 1:
                n_l2_level += 1
            mesh.setdefault(l1, {}).setdefault(l2, {}).setdefault(l3, {})[ui] = name
            if len(parts) == 1:
                labels[l2] = name          # label of a level-2 code = descriptor with that tree number
            elif len(parts) == 2:
                labels[tn] = name          # label of a level-3 code
    stats = {"descriptors": n_desc, "descriptors_without_tree_number": n_no_tree,
             "level2_tree_numbers": n_l2_level,
             "uids_in_dictionary": len({u for d1 in mesh.values() for d2 in d1.values() for d3 in d2.values() for u in d3})}
    return mesh, labels, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xml_file")
    ap.add_argument("--year", help="MeSH year (default: digits in the file name, e.g. desc2026.xml -> 2026)")
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src", "meshlytics", "essentials"))
    args = ap.parse_args()
    year = args.year or (re.search(r"(\d{4})", os.path.basename(args.xml_file)) or [None, datetime.now().year])[1]
    mesh, labels, stats = build(args.xml_file)
    out_dict = os.path.join(args.out_dir, f"MeSH_Dictionary_{year}.json")
    with open(out_dict, "w", encoding="utf-8") as f:
        json.dump(mesh, f, indent=4, ensure_ascii=False)
    kc_path = os.path.join(args.out_dir, "keys_and_classes.json")
    if os.path.exists(kc_path):
        keep = os.path.join(args.out_dir, f"keys_and_classes_before_{year}.json")
        if not os.path.exists(keep):
            os.replace(kc_path, keep)
            print(f"[INFO] Previous keys_and_classes.json kept as {os.path.basename(keep)}")
    with open(kc_path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(labels.items())), f, indent=4, ensure_ascii=False)
    print(f"[INFO] MeSH {year}: {stats}")
    print(f"[INFO] Saved: {out_dict}\n[INFO] Saved: {kc_path}")


if __name__ == "__main__":
    main()
