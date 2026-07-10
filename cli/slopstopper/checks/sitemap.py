"""sitemap.xml completeness + drift audit (in-CLI implementation).

Crawls a live site from `/` and cross-checks the served discovery files
against the pages it actually reaches. Where `reliability:llms-txt` and
`reliability:robots-txt` assert those files *exist and are well-formed*,
this check asserts they are *complete and current* — a black-box
assertion that the served `sitemap.xml` mirrors the reachable site,
**independent of how the file is produced** (hand-authored or generated).

The headline guarantee: **is `sitemap.xml` an accurate, current mirror of
the reachable site?** A reachable page absent from the sitemap is an
incompleteness bug; a `<loc>` that 404s is stale drift. Both hard-fail.
`llms.txt` is a *curated* map, so its page gaps are advisory by default.

Why the check still matters when a site generates these files at build
time: dynamic generation *prevents* drift, but the generator can still
have bugs (a collection left out of the iterator, an over-aggressive
`draft` filter). Prevention + independent detection are complementary —
so the remediation advice recommends generating the file from the route
inventory rather than hand-patching the missing `<loc>`.

Both a flat `<urlset>` and a nested `<sitemapindex>` → child sitemaps
(`sitemap-0.xml`, …) are supported transparently; neither shape is
flagged. A flat sitemap is spec-valid — the index is the *optional*
scale-out mechanism past the 50k-URL / 50MB limit.

CLI surface:
  slopstopper run reliability:sitemap -- --url URL [--path /sitemap.xml]
        [--llms-path /llms.txt] [--max-pages N] [--ignore GLOB ...]
        [--strict-orphans] [--require-llms-complete]

Stdlib-only (urllib + html.parser + ElementTree). Writes
.ss/reports/sitemap/sitemap-report.{md,json}.

Configuration (.slopstopper.yml — all optional):

    reliability:
      sitemap:
        max_pages: 200              # crawl bound; hitting it is logged, not silent
        ignore_paths: []             # globs excluded from crawl + diff
        allow_orphans: true          # sitemap entries not internally linked → advisory
        require_llms_complete: false # escalate llms.txt page gaps to hard-fail
        sitemap_path: /sitemap.xml
        llms_path: /llms.txt

Env-var equivalents the CLI also honours (precedence: flag > env > config):
  SITEMAP_TEST_URL   base URL to audit (required)
  SITEMAP_PATH       path to the sitemap (default: /sitemap.xml)

See .slopstopper.yml.example for the canonical schema.

Exit codes:
  0 — sitemap present, complete and free of dead entries
  1 — failures detected, URL missing, or unsafe-scheme URL supplied
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from slopstopper import config, output
from slopstopper.checks.llms_txt import _extract_links
from slopstopper.discovery import SITEMAP_NS, _collect_from_urlset


REPORT_DIR = Path(".ss/reports/sitemap")
REPORT_MD = REPORT_DIR / "sitemap-report.md"
USER_AGENT = "SlopStopper-Sitemap-Check/1.0"
TIMEOUT_SECONDS = 15
ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PATH = "/sitemap.xml"
DEFAULT_LLMS_PATH = "/llms.txt"
DEFAULT_MAX_PAGES = 200
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

# Consumed by `slopstopper emit reliability:sitemap --target pr-comment`.
# No issue keys: the check's exit code fails the workflow, no
# main-branch issue is created.
META = {
    "report_path": str(REPORT_MD),
    "comment_discriminator": "🗺️ sitemap",
}


# ── safety ───────────────────────────────────────────────────────


def _require_safe_url(url: str) -> None:
    """Reject any URL whose scheme isn't http/https (blocks file:// SSRF)."""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(
            f"sitemap check refuses scheme {scheme!r} (only http/https allowed). url={url!r}"
        )


def _fetch(url: str) -> tuple[int, str, str]:
    _require_safe_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # nosec B310
        return (
            resp.status,
            resp.headers.get("Content-Type", ""),
            resp.read().decode("utf-8", errors="replace"),
        )


def _head_ok(url: str) -> tuple[bool, str]:
    """Return (ok, detail) for a target — reachable and non-4xx/5xx."""
    try:
        _require_safe_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # nosec B310
            if resp.status >= 400:
                return False, f"HTTP {resp.status}"
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"


# ── path helpers ─────────────────────────────────────────────────


def _origin(url: str) -> tuple[str, str]:
    p = urllib.parse.urlparse(url)
    return p.scheme.lower(), p.netloc.lower()


def _same_origin(base: str, url: str) -> bool:
    return _origin(base) == _origin(url)


def _normalise_path(path: str) -> str:
    """Canonicalise a path for set comparison.

    `/index.html` → `/`, a trailing `index.html` is dropped, and a trailing
    slash is stripped from everything but the root. Applied identically to
    crawled and sitemap paths so trailing-slash / index.html differences
    wash out of the diff.
    """
    if not path:
        return "/"
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def _ignored(path: str, ignore_paths: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, glob) for glob in ignore_paths)


def _rebase(base: str, loc: str) -> str:
    """Rebase a sitemap/llms `<loc>` onto the test origin.

    Sitemaps carry absolute production URLs even when served from a local
    build, so audit them against the origin under test — never HEAD prod
    from a localhost run.
    """
    return urllib.parse.urljoin(base.rstrip("/") + "/", urllib.parse.urlparse(loc).path.lstrip("/"))


# ── crawl ────────────────────────────────────────────────────────


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.hrefs.append(value)


def _extract_hrefs(body: str) -> list[str]:
    parser = _HrefCollector()
    parser.feed(body)
    return parser.hrefs


def _is_html(ctype: str) -> bool:
    return any(ct in ctype.lower() for ct in HTML_CONTENT_TYPES)


def _crawl(base: str, max_pages: int, ignore_paths: list[str]) -> tuple[set[str], bool]:
    """BFS from `/`, following same-origin `<a href>` links.

    Returns (set of normalised HTML page paths, whether max_pages was hit).
    Only server-rendered links are seen — a JS-rendered SPA under-crawls;
    point the check at built static output for those.
    """
    start = urllib.parse.urljoin(base.rstrip("/") + "/", "")
    queue: list[str] = [start]
    visited: set[str] = set()
    crawled: set[str] = set()
    capped = False

    while queue:
        url = queue.pop(0)
        path = _normalise_path(urllib.parse.urlparse(url).path or "/")
        if path in visited:
            continue
        visited.add(path)

        try:
            status, ctype, body = _fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            continue
        if status != 200 or not _is_html(ctype):
            continue

        crawled.add(path)
        if len(crawled) >= max_pages:
            capped = True
            break

        for href in _extract_hrefs(body):
            target = urllib.parse.urljoin(url, href)
            parsed = urllib.parse.urlparse(target)
            if parsed.scheme.lower() not in ALLOWED_SCHEMES or not _same_origin(base, target):
                continue
            npath = _normalise_path(parsed.path or "/")
            if npath in visited or _ignored(npath, ignore_paths):
                continue
            queue.append(parsed._replace(query="", fragment="").geturl())

    return crawled, capped


# ── sitemap parsing ──────────────────────────────────────────────


def _collect_sitemap(base: str, sitemap_url: str, seen: set[str]) -> tuple[set[str], str | None]:
    """Fetch + parse a sitemap, recursing into index files over HTTP.

    Returns (set of normalised paths, error) — error is a string on an
    unreachable / invalid sitemap (a hard-fail), else None. Child sitemap
    URLs are rebased onto the test origin.
    """
    if sitemap_url in seen:
        return set(), None
    seen.add(sitemap_url)

    try:
        status, _ctype, body = _fetch(sitemap_url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        return set(), f"sitemap not reachable: {type(e).__name__}: {e}"
    if status != 200:
        return set(), f"HTTP status {status} (expected 200)"

    try:
        # Encode so ElementTree accepts the `<?xml encoding=...?>` declaration.
        # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
        root = ET.fromstring(body.encode("utf-8"))  # nosec B314
    except ET.ParseError as e:
        return set(), f"invalid sitemap XML: {e}"

    if root.tag == f"{SITEMAP_NS}sitemapindex":
        paths: set[str] = set()
        for sitemap_el in root.findall(f"{SITEMAP_NS}sitemap"):
            loc = sitemap_el.find(f"{SITEMAP_NS}loc")
            if loc is None or not loc.text:
                continue
            child_paths, err = _collect_sitemap(base, _rebase(base, loc.text.strip()), seen)
            if err:
                return set(), err
            paths |= child_paths
        return paths, None
    if root.tag == f"{SITEMAP_NS}urlset":
        return {_normalise_path(p) for p in _collect_from_urlset(root)}, None
    return set(), f"unexpected root element {root.tag!r} (expected urlset or sitemapindex)"


# ── llms.txt parsing ─────────────────────────────────────────────


def _llms_paths(base: str, llms_path: str) -> tuple[set[str] | None, str | None]:
    """Return (set of normalised link paths, error).

    A missing / unreachable llms.txt yields (None, note) — the `llms-txt`
    check owns its existence, so here it's advisory only.

    Links are compared by path, not origin: llms.txt commonly points at its
    pages with absolute production URLs, so an origin filter would wrongly
    drop every internal link on a localhost run (and behave differently in
    CI vs prod). External links (github, other sites) simply won't share a
    path with a crawled page, so they don't create false coverage.
    """
    url = urllib.parse.urljoin(base.rstrip("/") + "/", llms_path.lstrip("/"))
    try:
        status, _ctype, body = _fetch(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        return None, f"llms.txt not reachable ({type(e).__name__}) — skipping llms cross-check"
    if status != 200:
        return None, f"llms.txt returned HTTP {status} — skipping llms cross-check"

    paths: set[str] = set()
    for link in _extract_links(body):
        target = urllib.parse.urljoin(url, link)
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            continue
        paths.add(_normalise_path(parsed.path or "/"))
    return paths, None


# ── audit ────────────────────────────────────────────────────────


def _audit(
    base: str,
    sitemap_path: str,
    llms_path: str,
    max_pages: int,
    ignore_paths: list[str],
    allow_orphans: bool,
    require_llms_complete: bool,
) -> dict:
    _require_safe_url(base)  # fail fast; the crawl/collect loops swallow scheme errors
    issues: list[str] = []
    notes: list[str] = []

    crawled, capped = _crawl(base, max_pages, ignore_paths)
    if capped:
        output.warn(f"Crawl hit max_pages={max_pages}; diff covers the first {len(crawled)} pages only")
        notes.append(f"Crawl capped at max_pages={max_pages}; pages beyond the cap were not checked")

    sitemap_url = urllib.parse.urljoin(base.rstrip("/") + "/", sitemap_path.lstrip("/"))
    sitemap_paths, sitemap_err = _collect_sitemap(base, sitemap_url, set())

    if sitemap_err:
        issues.append(sitemap_err)
        return {
            "url": base,
            "sitemap_url": sitemap_url,
            "status": "fail",
            "issues": issues,
            "notes": notes,
            "crawled_count": len(crawled),
            "sitemap_count": 0,
            "missing_pages": [],
            "dead_entries": [],
            "orphan_entries": [],
            "llms_gaps": [],
        }

    sitemap_paths = {p for p in sitemap_paths if not _ignored(p, ignore_paths)}

    # crawled − sitemap → reachable but unlisted → incompleteness (hard-fail)
    missing_pages = sorted(crawled - sitemap_paths)
    for path in missing_pages:
        issues.append(f"Reachable page missing from sitemap.xml: {path}")

    # sitemap − crawled → dead entry (hard-fail) or orphan (advisory)
    dead_entries: list[dict] = []
    orphan_entries: list[str] = []
    for path in sorted(sitemap_paths - crawled):
        ok, detail = _head_ok(urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/")))
        if not ok:
            dead_entries.append({"path": path, "detail": detail})
            issues.append(f"Stale sitemap entry (unreachable {detail}): {path}")
        else:
            orphan_entries.append(path)
            msg = f"Sitemap entry not internally linked (orphan): {path}"
            (notes if allow_orphans else issues).append(msg)

    # llms.txt cross-check (advisory unless require_llms_complete)
    llms_gaps: list[str] = []
    llms_paths, llms_err = _llms_paths(base, llms_path)
    if llms_err:
        notes.append(llms_err)
    elif llms_paths is not None:
        llms_gaps = sorted(crawled - llms_paths)
        for path in llms_gaps:
            msg = f"Reachable page not listed in llms.txt: {path}"
            (issues if require_llms_complete else notes).append(msg)

    return {
        "url": base,
        "sitemap_url": sitemap_url,
        "status": "fail" if issues else "pass",
        "issues": issues,
        "notes": notes,
        "crawled_count": len(crawled),
        "sitemap_count": len(sitemap_paths),
        "missing_pages": missing_pages,
        "dead_entries": dead_entries,
        "orphan_entries": orphan_entries,
        "llms_gaps": llms_gaps,
    }


# ── markdown report ──────────────────────────────────────────────


def _build_markdown_report(result: dict) -> str:
    lines: list[str] = []
    lines.append("# 🗺️ sitemap Report")
    lines.append("")
    lines.append(f"**Site:** {result['url']}")
    lines.append("")
    lines.append(f"**Sitemap:** {result['sitemap_url']}")
    lines.append("")
    lines.append(f"**Overall:** {'✅ PASS' if result['status'] == 'pass' else '❌ FAIL'}")
    lines.append("")
    lines.append(
        f"**Pages crawled:** {result['crawled_count']} · "
        f"**Sitemap entries:** {result['sitemap_count']}"
    )
    lines.append("")
    if result["issues"]:
        lines.append("**Issues:**")
        for issue in result["issues"]:
            lines.append(f"- ❌ {issue}")
        lines.append("")
    if result["notes"]:
        lines.append("**Notes:**")
        for note in result["notes"]:
            lines.append(f"- ⚠️  {note}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## How to Fix")
    lines.append("")
    lines.append(
        "- **The durable fix — generate, don't hand-edit.** Missing and stale entries "
        "mean your sitemap has drifted from your routes. Rather than hand-patching each "
        "`<loc>`, generate `sitemap.xml` (and `llms.txt`) from your route inventory at "
        "build time — a framework sitemap integration (e.g. `@astrojs/sitemap`) or a "
        "build-time endpoint that iterates your content. A generated file can't drift. "
        "See [docs/reliability/SITEMAP.md](../../../docs/reliability/SITEMAP.md)."
    )
    lines.append(
        "- **Reachable page missing from sitemap** → add it (or, better, regenerate — see "
        "above). This is the incompleteness the check guards against."
    )
    lines.append(
        "- **Stale sitemap entry (unreachable)** → the `<loc>` 404s; remove the deleted "
        "page or fix its URL."
    )
    lines.append(
        "- **Orphan** → a real page that nothing links to. Add an internal link, or accept "
        "it (`reliability.sitemap.allow_orphans: true`, the default, keeps orphans advisory)."
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def _write_reports(result: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "sitemap-report.json").write_text(json.dumps(result, indent=2) + "\n")
    REPORT_MD.write_text(_build_markdown_report(result))


def _print_result(result: dict) -> None:
    icon = "✅" if result["status"] == "pass" else "❌"
    output._emit(f"  {icon} {result['url']} ({result['crawled_count']} pages crawled)")
    for issue in result["issues"]:
        output._emit(f"      - {issue}")


# ── CLI entrypoint ───────────────────────────────────────────────


def _parse_args(args: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="slopstopper run reliability:sitemap", add_help=False)
    p.add_argument("url_positional", nargs="?", default=None, help="Base URL to audit")
    p.add_argument("--url", default=None, help="Base URL to audit")
    p.add_argument("--path", default=None, help="Path to sitemap.xml (default: /sitemap.xml)")
    p.add_argument("--llms-path", default=None, help="Path to llms.txt (default: /llms.txt)")
    p.add_argument("--max-pages", type=int, default=None, help="Crawl bound (default: 200)")
    p.add_argument("--ignore", action="append", default=None, help="Glob to exclude (repeatable)")
    p.add_argument(
        "--strict-orphans",
        action="store_true",
        help="Fail on sitemap entries that nothing links to",
    )
    p.add_argument(
        "--require-llms-complete",
        action="store_true",
        help="Fail if a reachable page is missing from llms.txt",
    )
    p.add_argument("--help", "-h", action="help")
    return p.parse_args(args or [])


def _resolve_url(parsed_url: str | None) -> str | None:
    return parsed_url or os.environ.get("SITEMAP_TEST_URL")


def _resolve_path(parsed_path: str | None) -> str:
    return (
        parsed_path
        or os.environ.get("SITEMAP_PATH")
        or config.get("reliability.sitemap.sitemap_path")
        or DEFAULT_PATH
    )


def run(args: list[str] | None = None) -> int:
    parsed = _parse_args(args)
    url = _resolve_url(parsed.url_positional or parsed.url)
    if not url:
        output.error("sitemap target URL is required")
        output._emit("Usage:")
        output._emit("  slopstopper run reliability:sitemap -- --url https://your-site.example.com")
        output._emit("  SITEMAP_TEST_URL=https://your-site slopstopper run reliability:sitemap")
        return 1

    sitemap_path = _resolve_path(parsed.path)
    llms_path = parsed.llms_path or config.get("reliability.sitemap.llms_path") or DEFAULT_LLMS_PATH
    max_pages = parsed.max_pages or int(config.get("reliability.sitemap.max_pages", DEFAULT_MAX_PAGES))
    ignore_paths = list(parsed.ignore or config.get("reliability.sitemap.ignore_paths", []) or [])
    allow_orphans = not parsed.strict_orphans and bool(
        config.get("reliability.sitemap.allow_orphans", True)
    )
    require_llms_complete = parsed.require_llms_complete or bool(
        config.get("reliability.sitemap.require_llms_complete", False)
    )

    output.status("🗺️", f"sitemap completeness audit against: {url}")
    output.separator()

    try:
        result = _audit(
            url,
            sitemap_path,
            llms_path,
            max_pages,
            ignore_paths,
            allow_orphans,
            require_llms_complete,
        )
    except ValueError as e:
        output.error(str(e))
        return 1

    _write_reports(result)
    _print_result(result)

    output.separator()
    if result["status"] == "pass":
        output.success("sitemap.xml is complete and free of dead entries.")
        return 0
    output.error("Failures detected. See .ss/reports/sitemap/sitemap-report.md for full details.")
    return 1
