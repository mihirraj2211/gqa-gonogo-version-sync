#!/usr/bin/env python3
"""Work out why Confluence answers 404 for a page you can open in a browser.

A 404 from ``/wiki/api/v2/pages/{id}`` means the request authenticated but the
content was not returned, which has several quite different causes. This asks
the questions that separate them and prints a verdict:

  1. Does the token authenticate against Confluence at all? A token created for
     Jira with granular scopes will not read Confluence.
  2. Is the space visible to the token's own account, which may not be the
     account whose browser has the page open?
  3. Does the id exist under the v1 API, and is it actually a page? A whiteboard,
     database or embed has its own v2 endpoint and 404s under /pages/.
  4. What ids does a title search return? That finds the right one.

Usage:
    set -a && source .env && set +a
    python tools/diagnose_confluence.py            # uses CONFLUENCE_PAGE_ID
    python tools/diagnose_confluence.py 4137255661
"""

from __future__ import annotations

import os
import sys

import requests

TITLE_SEARCH = "Build GQA App Sign off"


def mask(secret: str) -> str:
    return f"{secret[:4]}...{secret[-4:]} ({len(secret)} chars)" if secret else "(empty)"


def main(argv: list[str]) -> int:
    domain = os.environ.get("CONFLUENCE_DOMAIN", "wbdstreaming.atlassian.net")
    email = os.environ.get("ATLASSIAN_USER_EMAIL", "")
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    page_id = argv[1] if len(argv) > 1 else os.environ.get("CONFLUENCE_PAGE_ID", "")

    if not email or not token:
        print("ATLASSIAN_USER_EMAIL / ATLASSIAN_API_TOKEN are not set.")
        print("Run: set -a && source .env && set +a")
        return 1

    base = f"https://{domain}"
    auth = (email, token)
    session = requests.Session()
    print(f"site      {base}")
    print(f"user      {email}")
    print(f"token     {mask(token)}")
    print(f"page id   {page_id}\n")

    unreachable: list[str] = []

    def call(label: str, path: str, **params):
        """Print one probe. Network failures are tracked apart from HTTP ones.

        Confusing "could not connect" with "refused the credential" is the very
        mistake this script exists to prevent.
        """
        try:
            response = session.get(f"{base}{path}", auth=auth, params=params, timeout=30)
        except requests.RequestException as exc:
            unreachable.append(label)
            print(f"{label:<22} unreachable: {type(exc).__name__}")
            return None
        body = response.text[:300].replace("\n", " ")
        print(f"{label:<22} {response.status_code}  {body}")
        if response.ok:
            try:
                return response.json()
            except ValueError:
                return None
        return None

    # 1. Who does Confluence think we are? 401/403 here means the token cannot
    #    read Confluence, whatever it can do in Jira.
    whoami = call("current user", "/wiki/rest/api/user/current")

    # 2. Which spaces can that account see?
    call("space GQA", "/wiki/api/v2/spaces", keys="GQA")

    # 3. The configured id, both API versions. v1 reporting a type other than
    #    'page' explains a v2 404 on its own.
    v2 = call("v2 pages/{id}", f"/wiki/api/v2/pages/{page_id}") if page_id else None
    v1 = call("v1 content/{id}", f"/wiki/rest/api/content/{page_id}", expand="space,version") if page_id else None

    # 4. Find the page by title instead.
    found = call(
        "title search",
        "/wiki/rest/api/content/search",
        cql=f'title~"{TITLE_SEARCH}" order by lastmodified desc',
        limit=10,
        expand="space",
    )

    print("\n--- verdict ---")
    if unreachable:
        print(f"Could not reach {domain} at all ({len(unreachable)} of 5 probes).")
        print("This says nothing about the token: check VPN, proxy and DNS first.")
        return 2

    if whoami is None:
        print("The token does not authenticate against Confluence.")
        print("Most likely it is a scoped token without Confluence access, or it")
        print("belongs to a different Atlassian account. Create a fresh token at")
        print("https://id.atlassian.com/manage-profile/security/api-tokens while")
        print("signed in as the account that can open the page.")
        return 1

    account = whoami.get("displayName") or whoami.get("email") or "unknown"
    print(f"Authenticated as {account}. Confluence access works.")

    if v2:
        print(f"Page {page_id} reads fine: {v2.get('title')!r}. Nothing to fix.")
        return 0

    if v1:
        kind = v1.get("type", "unknown")
        space = (v1.get("space") or {}).get("key", "?")
        print(f"v1 sees id {page_id} as type {kind!r} in space {space}.")
        if kind != "page":
            print(f"That is why /api/v2/pages/ 404s: a {kind} has its own v2 endpoint.")
        else:
            print("A v1 hit with a v2 miss usually means restricted view permissions.")
    else:
        print(f"Neither API can see id {page_id} as this account.")
        print("Either the id belongs to another site, or the page is restricted")
        print("or in the trash. Page restrictions return 404, not 403.")

    results = (found or {}).get("results") or []
    if results:
        print(f"\nPages matching {TITLE_SEARCH!r} that this account CAN see:")
        for item in results:
            space = (item.get("space") or {}).get("key", "?")
            print(f"  id={item.get('id'):<12} type={item.get('type'):<10} space={space:<6} {item.get('title')}")
        print("\nPut the right id in CONFLUENCE_PAGE_ID.")
    else:
        print(f"\nNo page matching {TITLE_SEARCH!r} is visible to this account either,")
        print("which points at permissions rather than a wrong id. Ask a space")
        print("admin to check view restrictions on the page and the GQA space.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
