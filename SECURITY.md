# Security policy

Meshlytics runs locally and calls public web services (NCBI E-utilities, OpenAlex, optionally OpenAI and NLM MeSH on
Demand) with credentials you provide.

## Reporting a problem

If you find a security problem, for example a way in which Meshlytics could expose API keys or send data to an
unexpected destination, please e-mail Ankush.Kesri@childrens.harvard.edu instead of opening a public issue.

## Protecting your credentials

- Keep API keys in environment variables or in a `config.json` outside version control (`config.json` is listed in
  `.gitignore`). Never paste keys into issues or pull requests.
- If a key was exposed, revoke it with the provider (NCBI account settings, OpenAlex, OpenAI) and create a new one.

Only the latest release receives fixes.
