# gqa-gonogo-version-sync

Keeps the **MAX Version Number** and **DPlus Version Number** columns of the GQA
Go/No-Go sign-off page in Confluence in step with the latest builds, on a
15-minute schedule, the same way the Fuse platform version dashboard tracks
builds.

Every 15 minutes the job reads the current builds per platform, derives the
per-brand version for each client row, and rewrites only those two columns on
the sign-off page. Everything else on the page — status macros, owners, notes,
rows this repo doesn't know about — is left exactly as it was.

## How it works

```
cron (*/15) -> GitHub Actions -> build API -> version mapping -> Confluence REST v2
```

1. **Fetch** the latest build per platform (`gonogo/providers.py`).
2. **Map** each platform onto a client row and derive the D+ version
   (`gonogo/versions.py`). MAX stays on the base `7.x.y.build` train. Apple,
   Android mobile and Android TV take the `+14` store offset and land on
   `21.x.y.build`; Web, CDEV, Chromecast and Roku stay on the base train. When
   the build API reports its own D+ build number, that number wins over the
   derived one, because D+ and MAX rarely share a build count.
3. **Update** the table and publish only if something actually changed
   (`gonogo/confluence.py`).

### Things it deliberately does

- **No empty-page revisions.** If the table already matches the builds, nothing
  is published. Otherwise you'd get 96 meaningless page versions a day.
- **No macro corruption.** Confluence storage format is parsed as XML, not HTML.
  An HTML round-trip rewrites `<ac:structured-macro/>` and Confluence rejects the
  result, which would break every macro on the page.
- **No blanking on a bad fetch.** A platform missing from the API is logged and
  skipped, never written as an empty cell.
- **No cross-train writes.** A `7.13.0.x` build will not be written to a `7.12.0`
  sign-off page while `release.enforce_train` is on.

## Setup

### 1. Repository secrets

`Settings -> Secrets and variables -> Actions -> Secrets`:

| Secret | What it is |
| --- | --- |
| `ATLASSIAN_USER_EMAIL` | Atlassian account that will edit the page |
| `ATLASSIAN_API_TOKEN` | [API token](https://id.atlassian.com/manage-profile/security/api-tokens) for that account |
| `FUSE_BUILDS_API_URL` | Build API endpoint (from the *Build Fetch API — curl Reference* page) |
| `FUSE_API_TOKEN` | Token for that endpoint, if it needs one |

### 2. Repository variables

`Settings -> Secrets and variables -> Actions -> Variables`:

| Variable | Default | What it is |
| --- | --- | --- |
| `SYNC_RUNNER` | `ubuntu-latest` | Runner label. Set to a self-hosted label if the build API is internal. |
| `CONFLUENCE_PAGE_ID` | from config | Page to update, so a new train doesn't need a code change |
| `CONFLUENCE_DOMAIN` | `wbdstreaming.atlassian.net` | Atlassian site |
| `RELEASE_TRAIN` | from config | e.g. `7.12.0` |

The account behind `ATLASSIAN_API_TOKEN` must have edit permission on the page.
A service account is better than a personal one: page history will show every
sync under that name.

### 3. Point it at your table

`config/clients.yml` holds the release train, the page id, the column headers to
look for, and the client rows. Row matching ignores case and punctuation, so
`Apple iOS / tvOS` in the table matches `apple ios tvos` in the config. Rows in
the table that aren't in the config are never touched.

## Running it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Offline rehearsal against the sample builds: prints what would change.
python -m gonogo.sync --builds-file tests/fixtures/sample_builds.json --dry-run

# Against the real build API, still without publishing.
cp .env.example .env    # fill in the tokens
set -a && source .env && set +a
python -m gonogo.sync --dry-run --verbose

# Publish for real.
python -m gonogo.sync
```

Useful flags: `--page-id` to target a different page, `--output body.xhtml` to
dump the storage format it would publish, `--builds-file` to replay a payload.

```bash
pytest    # 38 tests, no network needed
```

## Adapting the build API response

`config/clients.yml` describes the payload rather than hard-coding it:

```yaml
source:
  response:
    records_path: ""       # dot path to the list/map of builds ("" = root)
    platform_field: name
    version_field: version
    brand_field: brand     # omit if one record covers both brands
```

Both a flat list of records and a map keyed by platform are understood, and a
version embedded in an artifact filename (`max-android-7.12.0.66-release.apk`)
is extracted. If the real response needs shapes beyond that, `normalise_payload`
in `gonogo/providers.py` is the single place to change.

Switching to Artifactory instead is `BUILD_PROVIDER=jfrog` plus an
`source.options.aql_searches` block; the Confluence half stays untouched.

## Known limits

- **Cron drift.** GitHub runs scheduled workflows on shared capacity. `*/15` is a
  request, not a guarantee — runs are often minutes late and a tick can be
  skipped at peak times. For a Go/No-Go page this is fine; if you need exact
  15-minute ticks, a self-hosted cron is the way.
- **Network reachability is not the same as authentication.** GitHub-hosted
  runners live on the public internet. If the build API is only reachable from
  the WBD network, the job needs a self-hosted runner regardless of which tokens
  it holds.
- **Page history.** Every real change is a page version, attributed to the token
  owner, and will notify page watchers.
