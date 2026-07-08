# robots.txt Discoverability Check

`ss:reliability:robots-txt`, implemented in [`cli/slopstopper/checks/robots_txt.py`](../../cli/slopstopper/checks/robots_txt.py), audits your site's `/robots.txt` — the [Robots Exclusion Protocol](https://www.rfc-editor.org/rfc/rfc9309.html) file every crawler reads first. Its headline job is the **de-index guard**: a stray `Disallow: /` under `User-agent: *` (often leaked from a staging config) silently removes an entire public site from search — the single highest-blast-radius line a site can ship. It complements [`ss:reliability:seo`](SEO.md) (what human crawlers read) and [`ss:reliability:llms-txt`](LLMS_TXT.md) (what AI assistants read); together the three form the discovery-file triangle.

The check is Python stdlib only — no new dependencies on top of Python 3.

## What gets validated

The check fetches `/robots.txt` and asserts it is present and does not sabotage discoverability.

**Hard-fail (exit 1):**

- The file is reachable (HTTP 200)
- The body is non-empty
- The `User-agent: *` group has no blanket `Disallow: /` — escape hatch `allow_disallow_all`
- At least one `Sitemap:` directive is present

**Advisory (notes only, unless escalated via config):**

- Content-Type is `text/plain`
- An `Llms:` pointer to `/llms.txt` is present — escalate with `require_llms`
- When `check_links` is on, the `Sitemap:`/`Llms:` URLs are HEAD-verified as reachable (non-4xx/5xx)

Lighthouse's SEO audit (run by `ss:reliability:cwv`) already checks that robots.txt *parses*; this check surfaces the two failures Lighthouse buries — accidental blanket blocking and a missing sitemap pointer — as a named, first-class gate.

## Configuration

Read from environment variables or `.slopstopper.yml`'s `reliability.robots_txt` block:

| Variable / key | Default | Purpose |
| --- | --- | --- |
| `ROBOTS_TXT_TEST_URL` | (none, required) | Base URL to audit |
| `ROBOTS_TXT_PATH` | `/robots.txt` | Path to the file |
| `reliability.robots_txt.allow_disallow_all` | `false` | Permit a blanket `Disallow: /` (`--allow-disallow-all`) |
| `reliability.robots_txt.require_llms` | `false` | Fail if the `Llms:` pointer is missing (`--require-llms`) |
| `reliability.robots_txt.check_links` | `false` | HEAD-verify Sitemap:/Llms: URLs resolve (`--check-links`) |

## Running it

```bash
# Audit a deployed site
task ss:reliability:robots-txt -- https://your-site.example.com

# Audit the local build and verify the referenced URLs resolve
ROBOTS_TXT_TEST_URL=http://localhost:8080 \
task ss:reliability:robots-txt -- --check-links
```

Generated reports are written to:

- `.ss/reports/robots-txt/robots-txt-report.md` (human-readable)
- `.ss/reports/robots-txt/robots-txt-report.json` (machine-readable)

## Authoring a robots.txt

A minimal, healthy file for a public site:

```
User-agent: *
Allow: /

Sitemap: https://example.com/sitemap.xml
Llms: https://example.com/llms.txt
```

See [`app/robots.txt`](../../app/robots.txt) for this site's own file — it's the canonical example.

## Why this exists

A single wrong line in robots.txt can de-index a whole site, and the failure is silent — nothing errors, traffic just evaporates over the following weeks. Making it a first-class, deploy-time gate turns a slow, invisible catastrophe into a red check on the PR that introduced it. The sitemap-pointer assertion completes the discovery story: crawlers that read robots.txt find the URL inventory rather than guessing.

## CI integration

The [`ss-reliability-robots-txt-check.yml`](../../.github/workflows/ss-reliability-robots-txt-check.yml) workflow runs on every PR, every push to `main`, on `deployment_status` success, and daily. PR runs comment back with pass/fail and a link to the artefact.
