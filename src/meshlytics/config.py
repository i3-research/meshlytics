"""
config.py

Credentials and settings for Meshlytics.

Values are taken from (highest priority first):
  1. environment variables: NCBI_EMAIL, NCBI_API_KEY, OPENALEX_API_KEY, OPENAI_API_KEY, OPENAI_MODEL
  2. a JSON config file: the path given with `meshlytics --config <file>`, else the MESHLYTICS_CONFIG
     environment variable, else `config.json` in the current folder (see config.example.json)

The JSON file uses the keys: email, api_key, openalex_api_key, open_ai_api_key, openai_model.
Never commit a config file with real keys (config.json is listed in .gitignore).
"""

import json
import os

DEFAULT_OPENAI_MODEL = "gpt-4o"

# Module-level values used by the commands (filled by load())
email = None              # contact email sent to NCBI E-utilities (required by NCBI for fetch / top-cited)
api_key = None            # NCBI API key (optional; raises the limit from 3 to 10 requests per second)
openalex_api_key = None   # OpenAlex API key (top-cited)
open_ai_api_key = None    # OpenAI API key (institution-processing)
openai_model = DEFAULT_OPENAI_MODEL
config_file = None        # path of the config file that was read, if any

_ENV = {
    "email": "NCBI_EMAIL",
    "api_key": "NCBI_API_KEY",
    "openalex_api_key": "OPENALEX_API_KEY",
    "open_ai_api_key": "OPENAI_API_KEY",
    "openai_model": "OPENAI_MODEL",
}


def _find_config_file(path=None):
    """Returns the config file to read: explicit path, else MESHLYTICS_CONFIG, else ./config.json (or None)."""
    if path:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        return os.path.abspath(path)
    env_path = os.environ.get("MESHLYTICS_CONFIG")
    if env_path:
        if not os.path.isfile(env_path):
            raise FileNotFoundError(f"MESHLYTICS_CONFIG points to a missing file: {env_path}")
        return os.path.abspath(env_path)
    local = os.path.join(os.getcwd(), "config.json")
    return local if os.path.isfile(local) else None


def load(path=None):
    """
    Loads the settings into the module-level variables and returns them as a dict.
    Environment variables override values from the config file.
    """
    global email, api_key, openalex_api_key, open_ai_api_key, openai_model, config_file
    values = {"email": None, "api_key": None, "openalex_api_key": None, "open_ai_api_key": None,
              "openai_model": DEFAULT_OPENAI_MODEL}
    config_file = _find_config_file(path)
    if config_file:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in values:
            if data.get(key):
                values[key] = data[key]
        # placeholder from the example file is not a real address
        if values["email"] in ("user@example.com", "you@institution.edu"):
            values["email"] = None
    for key, env_name in _ENV.items():
        if os.environ.get(env_name):
            values[key] = os.environ[env_name]
    email, api_key = values["email"], values["api_key"]
    openalex_api_key, open_ai_api_key = values["openalex_api_key"], values["open_ai_api_key"]
    openai_model = values["openai_model"]
    return values
