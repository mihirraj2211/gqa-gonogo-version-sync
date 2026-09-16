"""Print the Build Fetch API curl reference page, using the tokens in .env.

Lists the children of the Fuse platform version dashboard and dumps the body of
the API/curl page among them, so the endpoint, headers and query parameters can
be copied into config/clients.yml.

Credentials are masked on the way out: if the page itself carries a real token,
it will not end up in a terminal log or a pasted snippet.

    python tools/read_curl_reference.py [dashboard_page_id]
"""

import re
import sys
from pathlib import Path

import requests

#: "Fuse platform version dashboard" in the GQA space; the curl reference is a child.
DASHBOARD_ID = sys.argv[1] if len(sys.argv) > 1 else "1306410872"


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def mask(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<REDACTED>")
    # Bearer/Basic values, long opaque strings, and ATATT-style Atlassian tokens.
    text = re.sub(r"(?i)(bearer|basic|token)\s+[A-Za-z0-9\-._~+/=]{12,}", r"\1 <REDACTED>", text)
    text = re.sub(r"ATATT[A-Za-z0-9\-._~+/=]+", "<REDACTED>", text)
    text = re.sub(r"(?i)(api[-_]?key|apikey|x-api-key|authorization)(\"?\s*[:=]\s*\"?)[^\"\s,}]{12,}",
                  r"\1\2<REDACTED>", text)
    return text


env_path = Path(__file__).resolve().parents[1] / ".env"
if not env_path.exists():
    sys.exit(f"no {env_path}; copy .env.example and fill in the Atlassian values")
env = load_env(env_path)
domain = env.get("CONFLUENCE_DOMAIN", "wbdstreaming.atlassian.net")
email = env.get("ATLASSIAN_USER_EMAIL", "")
token = env.get("ATLASSIAN_API_TOKEN", "")
if not email or not token:
    sys.exit("ATLASSIAN_USER_EMAIL / ATLASSIAN_API_TOKEN missing from .env")

session = requests.Session()
session.auth = (email, token)
base = f"https://{domain}/wiki/api/v2"
secrets = [token]


def get(path: str, **params):
    response = session.get(f"{base}{path}", params=params, headers={"Accept": "application/json"}, timeout=30)
    if response.status_code >= 400:
        sys.exit(f"{path} -> {response.status_code}: {mask(response.text[:300], secrets)}")
    return response.json()


children = get(f"/pages/{DASHBOARD_ID}/children", limit=100).get("results", [])
print("== children of the Fuse platform version dashboard ==")
for child in children:
    print(f"  {child['id']}  {child['title']}")

targets = [c for c in children if "curl" in c["title"].lower() or "api" in c["title"].lower()]
if not targets:
    targets = children

for target in targets:
    page = get(f"/pages/{target['id']}", **{"body-format": "storage"})
    body = page["body"]["storage"]["value"]
    print(f"\n{'=' * 70}\n== {page['title']}  (id {page['id']}, {len(body)} chars)\n{'=' * 70}")
    print(mask(body, secrets))
