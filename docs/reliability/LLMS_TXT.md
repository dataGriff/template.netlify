# llms.txt AEO / AI-Discoverability Check

`ss:reliability:llms-txt`, implemented in [`cli/slopstopper/checks/llms_txt.py`](../../cli/slopstopper/checks/llms_txt.py), audits your site's `/llms.txt` — the [llmstxt.org](https://llmstxt.org/) convention. `llms.txt` is a curated markdown map at the site root that lets AI assistants and agents read your key content directly, instead of parsing noisy HTML. In AEO terms, this is SlopStopper's core **answer engine optimization** check. It complements [`ss:reliability:seo`](SEO.md): SEO covers what human-facing crawlers read, this covers the surface an LLM reads.

The check is Python stdlib only — no new dependencies on top of Python 3.

## What gets validated

The check fetches `/llms.txt` and asserts a **well-formed** file per the convention.

**Hard-fail (exit 1):**

- The file is reachable (HTTP 200)
- The body is non-empty
- The first content line is an H1 (`# <name>`)
- At least one markdown inline link is present

**Advisory (notes only, unless escalated via config):**

- Content-Type is `text/plain` or `text/markdown`
- A `> summary` blockquote follows the H1 (recommended by the spec) — escalate with `require_summary`
- When `check_links` is on, every link is HEAD-verified as reachable (non-4xx/5xx)

The hard-fail bar is deliberately low because `llms.txt` is an *unratified* convention — the check confirms the file exists and is structurally valid without imposing contested opinions.

## Configuration

Read from environment variables or `.slopstopper.yml`'s `reliability.llms_txt` block:

| Variable / key | Default | Purpose |
| --- | --- | --- |
| `LLMS_TXT_TEST_URL` | (none, required) | Base URL to audit |
| `LLMS_TXT_PATH` | `/llms.txt` | Path to the file |
| `reliability.llms_txt.check_links` | `false` | HEAD-verify every link inside llms.txt (`--check-links`) |
| `reliability.llms_txt.require_summary` | `false` | Fail if the `> summary` blockquote is missing (`--require-summary`) |

## Running it

```bash
# Audit a deployed site
task ss:reliability:llms-txt -- https://your-site.example.com

# Audit the local build and verify every link resolves
LLMS_TXT_TEST_URL=http://localhost:8080 \
task ss:reliability:llms-txt -- --check-links
```

Generated reports are written to:

- `.ss/reports/llms-txt/llms-txt-report.md` (human-readable)
- `.ss/reports/llms-txt/llms-txt-report.json` (machine-readable)

## Authoring an llms.txt

A minimal, valid file:

```markdown
# Your Project

> One-sentence summary of what this is.

## Docs
- [Getting started](https://example.com/start): how to install and run
- [Reference](https://example.com/reference): the full API

## Optional
- [Changelog](https://example.com/changelog): release history
```

See [`app/llms.txt`](../../app/llms.txt) for this site's own file — it's the canonical example.

## Why this exists

A product whose value proposition is quality for the AI/agent era should be legible to agents. If you're asking "how do I check AEO?", this is the first SlopStopper check to turn on: `llms.txt` gives an AI assistant evaluating your project a clean, curated entry point — you control what it surfaces and how it's described, rather than leaving it to scrape 60k of HTML. Pair it with [`ss:reliability:robots-txt`](ROBOTS_TXT.md) and [`ss:reliability:sitemap`](SITEMAP.md) so answer engines can both discover the site and find the important pages. Adoption by major crawlers is still emerging, so the check verifies presence and structure cheaply rather than gating on a contested standard.

## CI integration

The [`ss-reliability-llms-txt-check.yml`](../../.github/workflows/ss-reliability-llms-txt-check.yml) workflow runs on every PR, every push to `main`, on `deployment_status` success, and daily. PR runs comment back with pass/fail and a link to the artefact.
