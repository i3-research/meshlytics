"""
dependencies.py

Start-up check for the third-party libraries Meshlytics needs.

The libraries are installed with the package (`pip install meshlytics` or `pip install .`).
This check only gives a clear message instead of a traceback when one is missing, e.g. in a
manually managed environment. It does not install anything.
"""

import importlib.util
import sys

# pip package name : import name
REQUIRED_LIBRARIES = {
    "pandas": "pandas",
    "numpy": "numpy",
    "requests": "requests",
    "biopython": "Bio",
    "openpyxl": "openpyxl",
    "ratelimit": "ratelimit",
    "matplotlib": "matplotlib",
    "scipy": "scipy",
    "networkx": "networkx",
    "adjustText": "adjustText",
    "wordcloud": "wordcloud",
    "tqdm": "tqdm",
}

# needed only by `institution-processing` (pip install "meshlytics[llm]")
LLM_LIBRARIES = {
    "openai": "openai",
    "xlsxwriter": "xlsxwriter",
}


def missing_libraries(libraries):
    """Returns the pip names of the libraries that cannot be imported."""
    return [pip_name for pip_name, import_name in libraries.items() if importlib.util.find_spec(import_name) is None]


def ensure_required_libraries(include_llm=False):
    """Exits with an install hint if a required library is missing."""
    missing = missing_libraries(REQUIRED_LIBRARIES)
    hint = "pip install meshlytics"
    if include_llm:
        missing += missing_libraries(LLM_LIBRARIES)
        hint = 'pip install "meshlytics[llm]"'
    if missing:
        sys.stderr.write(f"[ERROR] Missing Python libraries: {', '.join(missing)}\n"
                         f"        Install them with: {hint}  (or: pip install {' '.join(missing)})\n")
        sys.exit(1)
