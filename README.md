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

1. **Read the page** (`gonogo/confluence.py`) and take the release train from its
   title, so a 7.13.0 build can never land on a 7.12.0 sign-off page.
2. **Fetch** the latest build per platform (`gonogo/providers.py`).
3. **Map** each platform onto a client row and derive the D+ version
   (`gonogo/versions.py`). MAX stays on the base `7.x.y.build` train. Apple,
   Android mobile and Android TV take the `+14` store offset and land on
   `21.x.y.build`; Web, CDEV, Chromecast and Roku stay on the base train. When
   the build API reports its own D+ build number, that number wins over the
   derived one, because D+ and MAX rarely share a build count.
4. **Update** the table and publish only if something actually changed.

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
- **No lost race.** If someone edits the page between the read and the publish,
  Confluence answers 409; the run re-reads and reapplies instead of waiting for
  the next tick.

## Plugging in your build API

The Confluence half is finished and tested. The only thing that varies is the
shape of the build feed, so nothing about it is hard-coded: `config/clients.yml`
describes the request and the response, and `gonogo/probe.py` tells you what to
put there.

With the endpoint from the *Build Fetch API — curl Reference* page (child of the
Fuse platform version dashboard):

```bash
export FUSE_BUILDS_API_URL='https://.../builds?env=orange'
export FUSE_API_TOKEN='...'
export ATLASSIAN_USER_EMAIL='you@wbd.com' ATLASSIAN_API_TOKEN='...'

# 1. See the real payload, a suggested mapping, and whether the page lines up.
python -m gonogo.probe --show-payload

# 2. Paste the suggested block into config/clients.yml under source.response,
#    then confirm every platform and row resolves.
python -m gonogo.probe

# 3. Rehearse the write, then publish.
python -m gonogo.sync --dry-run
python -m gonogo.sync
```

The probe writes to Confluence never, and prints:

- the exact requests it made (URL, query, brand),
- the raw payload with `--show-payload`,
- a `source.response` block guessed from that payload,
- the platform keys parsed, with the MAX and D+ version found for each,
- configured platforms missing from the payload, and keys the config ignores,
- the page's column headers and client rows, and which config rows matched.

### Request shapes the config already covers

```yaml
source:
  auth: bearer            # bearer | token | basic | jfrog | header | none
  method: get             # post sends `body` as JSON, for query-style endpoints
  query:
    env: orange
    date: "{today:%d/%m/%Y}"   # expanded per run, like the dashboard's date box
  # Only when the API serves one brand per call, like the MAX / HBOMAX selector:
  brand_requests:
    - { brand: max,   query: { brand: MAX } }
    - { brand: dplus, query: { brand: DPLUS } }
  response:
    records_path: "data.systems"   # dot path to the records ("" = root)
    platform_field: name
    version_field: version
    brand_field: brand             # omit if one record covers both brands
    max_brand_value: [max, hbomax]
    dplus_brand_value: [dplus, discovery]
    brand_versions_field: versions # when one record nests both brands
    built_at_field: buildDate
```

Understood payloads: a flat list of records, a map keyed by platform, records
nesting a `{brand: version}` block, one request per brand, and a version
embedded in an artifact filename (`max-android-7.12.0.66-release.apk`). Date
tokens are `{today}`, `{yesterday}`, `{tomorrow}`, `{now}` and `{epoch_ms}`,
each accepting a strftime format, and they work in query values and anywhere in
a POST body. If a real response needs a shape beyond that, `collect_builds` in
`gonogo/providers.py` is the single place to change.

Switching to Artifactory instead is `BUILD_PROVIDER=jfrog` plus a
`source.options.aql_searches` block; the Confluence half stays untouched.

## Setup

### 1. Repository secrets

`Settings -> Secrets and variables -> Actions -> Secrets`:

| Secret | What it is |
| --- | --- |
| `ATLASSIAN_USER_EMAIL` | Atlassian account that will edit the page |
| `ATLASSIAN_API_TOKEN` | [API token](https://id.atlassian.com/manage-profile/security/api-tokens) for that account |
| `FUSE_BUILDS_API_URL` | Build API endpoint (from the *Build Fetch API — curl Reference* page) |
| `FUSE_API_TOKEN` | Token for that endpoint, if it needs one |
| `JFROG_URL` / `JFROG_TOKEN` | Only for `BUILD_PROVIDER=jfrog` |

The account behind `ATLASSIAN_API_TOKEN` must have edit permission on the page.
A service account is better than a personal one: page history will show every
sync under that name.

### 2. Repository variables

`Settings -> Secrets and variables -> Actions -> Variables`:

| Variable | Default | What it is |
| --- | --- | --- |
| `SYNC_RUNNER` | `ubuntu-latest` | Runner label. Set to a self-hosted label if the build API is internal. |
| `CONFLUENCE_PAGE_ID` | from config | Page to update, so a new train doesn't need a code change |
| `CONFLUENCE_DOMAIN` | `wbdstreaming.atlassian.net` | Atlassian site |
| `RELEASE_TRAIN` | from page title | Pin the train instead of reading it from the title |
| `ENFORCE_TRAIN` | `true` | Set `false` to accept builds from any train |
| `BUILD_PROVIDER` | `fuse_api` | `fuse_api`, `grafana` or `jfrog` |
| `MIN_PLATFORMS` / `MIN_ROWS` | `1` | Health check fails below these counts |

### 3. Point it at your table

`config/clients.yml` holds the release train, the page id, the column headers to
look for, and the client rows. Row matching ignores case and punctuation, so
`Apple iOS / tvOS` in the table matches `apple ios tvos` in the config. Rows in
the table that aren't in the config are never touched.

### Which train a run will write

In order of precedence: the `RELEASE_TRAIN` variable (or `--release-train`), then
the version in the page title, then `release.train` in the config. So moving to
the next train is a `CONFLUENCE_PAGE_ID` change, not a code change — and if the
feed has moved on while the page has not, the run fails with that stated
explicitly rather than silently skipping every row.

## Scheduled jobs

| Workflow | Schedule | What it does |
| --- | --- | --- |
| `sync-signoff-versions.yml` | `*/15 * * * *` | The sync. Manual runs default to a dry run and accept a page id and train. |
| `build-api-health.yml` | `25 */6 * * *` | Runs the probe. Fails if the API or page is unreachable, or if coverage falls below `MIN_PLATFORMS` / `MIN_ROWS`. |
| `tests.yml` | `40 5 * * 1` | Tests plus a credential-free rehearsal of the whole pipeline against fixtures. |

The health check exists because the sync can succeed while doing nothing useful:
a renamed row, a platform that stopped reporting or an expiring token all look
like warnings in a green run. The probe turns those into a red one.

## Running it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Offline rehearsal: no tokens, no network, no writes.
python -m gonogo.sync --builds-file tests/fixtures/sample_builds.json \
                      --page-file tests/fixtures/sample_page.xhtml

# Against the real build API and the real page, still without publishing.
cp .env.example .env    # fill in the tokens
set -a && source .env && set +a
python -m gonogo.sync --dry-run --verbose

# Publish for real.
python -m gonogo.sync
```

Useful flags: `--page-id` to target a different page, `--page-file` to work from
a saved body, `--output body.xhtml` to dump the storage format it would publish,
`--builds-file` to replay a payload, `--release-train` to override the train.

```bash
pytest    # 72 tests, no network needed
```

## Known limits

- **Cron drift.** GitHub runs scheduled workflows on shared capacity. `*/15` is a
  request, not a guarantee — runs are often minutes late and a tick can be
  skipped at peak times. For a Go/No-Go page this is fine; if you need exact
  15-minute ticks, a self-hosted cron is the way.
- **Scheduled workflows go dormant.** GitHub disables schedules in a repository
  with no activity for 60 days. The weekly test run keeps this one awake.
- **Network reachability is not the same as authentication.** GitHub-hosted
  runners live on the public internet. If the build API is only reachable from
  the WBD network, the job needs a self-hosted runner regardless of which tokens
  it holds.
- **Page history.** Every real change is a page version, attributed to the token
  owner, and will notify page watchers.
