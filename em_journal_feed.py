#!/usr/bin/env python3
"""
em_journal_feed.py — Merged RSS feed of new articles from your journals,
with links routed through JHU's EZproxy so you land on full text.

How it works:
  1. Queries PubMed E-utilities for each journal in JOURNALS (last LOOKBACK_DAYS days)
  2. Builds a single RSS 2.0 feed, newest first
  3. Each item links to the article via the Welch/JHU proxy prefix
     (DOI link when available, PubMed link as fallback)

No dependencies — uses only the Python standard library (macOS python3 is fine).

Usage:
  python3 em_journal_feed.py            # writes feed.xml next to the script
  python3 em_journal_feed.py /path/out.xml

Also writes feeds/<journal>.xml (one feed per journal) and subscriptions.opml
(import once into Reeder / NetNewsWire to subscribe to everything, in folders).

Point your RSS reader (NetNewsWire, Reeder, etc.) at the output file, or have
launchd/Hazel push it somewhere with a URL (iCloud, GitHub Pages, your NAS).
"""

import os
import re
import sys
import time
import json
import html
import datetime
import email.utils
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ============================== CONFIG ======================================

# JHU EZproxy prefix. Must be "qurl=" (not "url="): the target is appended
# percent-encoded, and EZproxy only decodes it under qurl. With url= + an
# encoded target, EZproxy can't parse it and dumps you on its menu page.
# "https://proxy.library.jhu.edu/login?qurl=" also works (it just 302s to proxy1).
PROXY_PREFIX = "https://proxy1.library.jhu.edu/login?qurl="

# Where the feeds are served from (GitHub Pages). Only used for the URLs inside
# subscriptions.opml — change it if the feeds ever move to another host.
PAGES_BASE_URL = "https://peterfredericks.github.io/journal-feed"

# Exact PubMed journal names (searched as "<name>"[Journal]), grouped into the
# folders your RSS reader will show after importing subscriptions.opml.
# Edit freely — add/remove lines. A wrong name shows up as 0 articles forever.
JOURNAL_GROUPS = {
    "Core EM": [
        "Annals of emergency medicine",
        "Academic emergency medicine : official journal of the Society for Academic Emergency Medicine",
        "Journal of the American College of Emergency Physicians open",
        "The American journal of emergency medicine",
        "The Journal of emergency medicine",
        "Emergency medicine journal : EMJ",
        "The western journal of emergency medicine",
        "Resuscitation",
        "Prehospital emergency care",
    ],
    "Critical Care": [
        "Critical care medicine",
        "Intensive care medicine",
        "Critical care (London, England)",
        "American journal of respiratory and critical care medicine",
        "Chest",
    ],
    "General": [
        "The New England journal of medicine",
        "JAMA",
        "Lancet (London, England)",
        "JAMA internal medicine",
        "Annals of internal medicine",
        "JAMA network open",
    ],
    "Addiction": [
        "Journal of addiction medicine",
        "Drug and alcohol dependence",
    ],
}
JOURNALS = [j for group in JOURNAL_GROUPS.values() for j in group]

# What each feed is called in your reader. Purely cosmetic — the PubMed names
# above drive the search and the feed URLs. Anything not listed falls back to
# its PubMed name.
DISPLAY_NAMES = {
    "Annals of emergency medicine": "Annals of Emergency Medicine",
    "Academic emergency medicine : official journal of the Society for Academic Emergency Medicine":
        "Academic Emergency Medicine",
    "Journal of the American College of Emergency Physicians open": "JACEP Open",
    "The American journal of emergency medicine": "American Journal of Emergency Medicine",
    "The Journal of emergency medicine": "Journal of Emergency Medicine",
    "Emergency medicine journal : EMJ": "Emergency Medicine Journal",
    "The western journal of emergency medicine": "Western Journal of Emergency Medicine",
    "Resuscitation": "Resuscitation",
    "Prehospital emergency care": "Prehospital Emergency Care",
    "Critical care medicine": "Critical Care Medicine",
    "Intensive care medicine": "Intensive Care Medicine",
    "Critical care (London, England)": "Critical Care",
    "American journal of respiratory and critical care medicine": "AJRCCM",
    "Chest": "CHEST",
    "The New England journal of medicine": "NEJM",
    "JAMA": "JAMA",
    "Lancet (London, England)": "The Lancet",
    "JAMA internal medicine": "JAMA Internal Medicine",
    "Annals of internal medicine": "Annals of Internal Medicine",
    "JAMA network open": "JAMA Network Open",
    "Journal of addiction medicine": "Journal of Addiction Medicine",
    "Drug and alcohol dependence": "Drug and Alcohol Dependence",
}

WRITE_PER_JOURNAL = True   # also write one feed per journal into a feeds/ folder
LOOKBACK_DAYS = 7          # how far back each run looks (feed readers dedupe by GUID)
MAX_PER_JOURNAL = 150      # safety cap per journal per run (JAMA Netw Open runs 40+/week)
FEED_TITLE = "All Journals"
FEED_DESC = "New articles from every journal, in one feed"
REQUEST_DELAY = 0.4        # seconds between NCBI calls (stay under 3 req/s)
MAX_RETRIES = 4            # per NCBI call, with exponential backoff (429 / 5xx / network)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL_PARAMS = {"tool": "personal_journal_feed", "email": ""}  # optionally add your email

# Optional: an NCBI API key in the NCBI_API_KEY environment variable (never in
# this file — the repo is public) raises the rate limit from 3 to 10 req/s.
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "").strip()
if NCBI_API_KEY:
    TOOL_PARAMS["api_key"] = NCBI_API_KEY
    REQUEST_DELAY = 0.12

# ============================================================================


def eutils_get(endpoint: str, params: dict) -> dict:
    params = {**params, **TOOL_PARAMS, "retmode": "json"}
    url = f"{EUTILS}/{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "journal-feed/1.0"})
    last_err = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(REQUEST_DELAY * 2 ** attempt)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                # strict=False: NCBI occasionally emits raw control chars in titles
                data = json.loads(resp.read().decode("utf-8"), strict=False)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            last_err = e
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
            last_err = e          # network blip or truncated/non-JSON body
            continue
        # NCBI reports some failures (rate limit, backend down) inside a 200 body
        err = data.get("error") or data.get("esearchresult", {}).get("ERROR") \
            or data.get("esummaryresult")
        if err:
            last_err = RuntimeError(f"NCBI error: {err}")
            continue
        return data
    raise last_err


def search_journal(journal: str) -> list[str]:
    """Return PMIDs for recent articles in a journal."""
    term = f'"{journal}"[Journal]'
    data = eutils_get("esearch.fcgi", {
        "db": "pubmed",
        "term": term,
        "datetype": "edat",
        "reldate": LOOKBACK_DAYS,
        "retmax": MAX_PER_JOURNAL,
        "sort": "date",
    })
    return data.get("esearchresult", {}).get("idlist", [])


def fetch_summaries(pmids: list[str]) -> list[dict]:
    """Return article summaries for a list of PMIDs."""
    if not pmids:
        return []
    data = eutils_get("esummary.fcgi", {"db": "pubmed", "id": ",".join(pmids)})
    result = data.get("result", {})
    articles = []
    for pmid in result.get("uids", []):
        rec = result.get(pmid, {})
        doi = ""
        for aid in rec.get("articleids", []):
            if aid.get("idtype") == "doi":
                doi = aid.get("value", "")
                break
        if rec.get("error"):
            continue
        # Entrez date = when PubMed added the record. Used as the item date:
        # sortpubdate is the *issue* date, often weeks in the future.
        edat = ""
        for h in rec.get("history", []):
            if h.get("pubstatus") == "entrez":
                edat = h.get("date", "")
                break
        authors = [a.get("name", "") for a in rec.get("authors", [])][:6]
        articles.append({
            "pmid": pmid,
            "title": clean_text(rec.get("title", "")).rstrip(".") or "[No title]",
            "journal": clean_text(rec.get("fulljournalname") or rec.get("source", "")),
            "abbrev": clean_text(rec.get("source", "")),   # e.g. "N Engl J Med"
            "authors": clean_text(", ".join(a for a in authors if a)),
            "pubdate": edat or rec.get("sortpubdate", "") or rec.get("epubdate", "") or rec.get("pubdate", ""),
            "doi": doi.strip(),
            "pubtypes": ", ".join(rec.get("pubtype", [])),
        })
    return articles


_TAGS = re.compile(r"</?(?:i|b|u|em|strong|sub|sup)\s*/?>", re.I)  # bare tags only: "a <b or c> d" is text
_XML_ILLEGAL = re.compile("[^\t\n\r\u0020-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]")


def clean_text(s) -> str:
    """Plain text from a PubMed field: decode entities, drop inline markup
    and characters that are illegal in XML 1.0, collapse whitespace."""
    s = html.unescape(str(s or ""))
    s = _TAGS.sub("", s)
    s = _XML_ILLEGAL.sub("", s)
    return " ".join(s.split())


def esc(s) -> str:
    """Escape text for XML, dropping characters XML 1.0 can't represent at all."""
    return html.escape(_XML_ILLEGAL.sub("", str(s)))


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def parse_date(s: str) -> datetime.datetime:
    """Parse a PubMed date (naive UTC). Never returns a future date — readers
    pin or hide future-dated items. Unparseable -> epoch-ish, so it sorts last
    and stays stable across runs."""
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y %b %d", "%Y %b", "%Y"):
        try:
            return min(datetime.datetime.strptime(str(s).strip(), fmt), utcnow())
        except (ValueError, AttributeError):
            continue
    return datetime.datetime(2000, 1, 1)


def rfc822(dt: datetime.datetime) -> str:
    # email.utils is locale-independent (strftime %a/%b is not)
    return email.utils.format_datetime(dt.replace(tzinfo=datetime.timezone.utc))


def slugify(name: str) -> str:
    """File slug for a journal: subtitle after ' : ' dropped, dashes collapsed."""
    base = name.split(" : ")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:60].strip("-")
    return slug or "journal"


def build_rss(articles: list[dict], title: str = FEED_TITLE, desc: str = FEED_DESC,
              self_url: str = "", show_journal: bool = False) -> str:
    # show_journal: prefix item titles with the journal's short name. Only the
    # merged feed wants this — inside a single-journal feed the reader already
    # shows the feed name on every item, so a prefix is pure clutter.
    # self_url = where this feed is published (feed validators recommend declaring it)
    self_link = f'\n    <atom:link href="{esc(self_url)}" rel="self" type="application/rss+xml"/>' if self_url else ""
    now = rfc822(utcnow())
    items = []
    for art in articles:
        pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{art['pmid']}/"
        target = f"https://doi.org/{art['doi']}" if art["doi"] else pubmed_url
        link = PROXY_PREFIX + urllib.parse.quote(target, safe="")
        proxied_pubmed = PROXY_PREFIX + urllib.parse.quote(pubmed_url, safe="")

        short = art.get("abbrev") or display_name(art["journal"])
        item_title = esc(f"{short}: {art['title']}" if show_journal and short else art["title"])
        desc_parts = [
            esc(art["authors"]) if art["authors"] else "",
            esc(art["pubtypes"]) if art["pubtypes"] else "",
            f'<a href="{esc(link)}">Full text (JHU proxy)</a>',
            f'<a href="{esc(proxied_pubmed)}">PubMed (proxied)</a>',
        ]
        item_desc = esc("<br/>".join(p for p in desc_parts if p))

        items.append(f"""    <item>
      <title>{item_title}</title>
      <link>{esc(link)}</link>
      <guid isPermaLink="false">pmid:{art['pmid']}</guid>
      <pubDate>{rfc822(parse_date(art['pubdate']))}</pubDate>
      <category>{esc(art['journal'])}</category>
      <description>{item_desc}</description>
    </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{esc(title)}</title>
    <link>https://pubmed.ncbi.nlm.nih.gov/</link>{self_link}
    <description>{esc(desc)}</description>
    <lastBuildDate>{now}</lastBuildDate>
    <ttl>180</ttl>
{chr(10).join(items)}
  </channel>
</rss>
"""


def display_name(journal: str) -> str:
    """Reader-facing name: DISPLAY_NAMES entry, else PubMed name minus ' : subtitle'."""
    return DISPLAY_NAMES.get(journal) or journal.split(" : ")[0]


def build_opml(groups: dict = None, base_url: str = None) -> str:
    """OPML 2.0 subscription list: merged feed on top, then one folder per group."""
    groups = JOURNAL_GROUPS if groups is None else groups
    base = (PAGES_BASE_URL if base_url is None else base_url).rstrip("/")

    def feed(indent: str, text: str, url: str) -> str:
        return (f'{indent}<outline type="rss" text="{esc(text)}" title="{esc(text)}" '
                f'xmlUrl="{esc(url)}" htmlUrl="https://pubmed.ncbi.nlm.nih.gov/"/>')

    lines = [feed("    ", FEED_TITLE, f"{base}/feed.xml")]
    for group, journals in groups.items():
        lines.append(f'    <outline text="{esc(group)}" title="{esc(group)}">')
        lines += [feed("      ", display_name(j), f"{base}/feeds/{slugify(j)}.xml") for j in journals]
        lines.append("    </outline>")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head>
    <title>{esc(FEED_TITLE)}</title>
    <dateCreated>{rfc822(utcnow())}</dateCreated>
  </head>
  <body>
{chr(10).join(lines)}
  </body>
</opml>
"""


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "feed.xml"
    slugs = [slugify(j) for j in JOURNALS]
    dupes = {x for x in slugs if slugs.count(x) > 1}
    if dupes:
        raise SystemExit(f"Journal names collide on feed filename: {sorted(dupes)}")

    by_journal: dict[str, list[dict]] = {}
    for journal in JOURNALS:
        try:
            pmids = search_journal(journal)
            time.sleep(REQUEST_DELAY)
            arts = fetch_summaries(pmids) if pmids else []
            if pmids:
                time.sleep(REQUEST_DELAY)
            by_journal[journal] = arts
            print(f"  {journal}: {len(arts)} article(s)")
        except Exception as e:
            print(f"  {journal}: ERROR {e}", file=sys.stderr)

    failed = [j for j in JOURNALS if j not in by_journal]
    if len(failed) == len(JOURNALS):
        # Total outage: leave the last good feeds in place rather than publish empties
        print("\nEvery journal failed — feeds NOT rewritten.", file=sys.stderr)
        return 1

    # Merged feed: dedupe by PMID, newest first
    seen, merged = set(), []
    for arts in by_journal.values():
        for art in arts:
            if art["pmid"] not in seen:
                seen.add(art["pmid"])
                merged.append(art)
    merged.sort(key=lambda a: parse_date(a["pubdate"]), reverse=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base = PAGES_BASE_URL.rstrip("/")
    out_path.write_text(build_rss(merged, self_url=f"{base}/feed.xml", show_journal=True), encoding="utf-8")
    print(f"\nWrote {len(merged)} items to {out_path} (merged)")

    # Per-journal feeds → feeds/<journal>.xml next to the merged feed
    if WRITE_PER_JOURNAL:
        feeds_dir = out_path.parent / "feeds"
        feeds_dir.mkdir(exist_ok=True)
        # A journal that errored keeps its previous feed file untouched
        for journal, arts in by_journal.items():
            arts = sorted(arts, key=lambda a: parse_date(a["pubdate"]), reverse=True)
            nice_name = display_name(journal)
            fp = feeds_dir / f"{slugify(journal)}.xml"
            fp.write_text(
                build_rss(arts, title=nice_name,
                          desc=f"New articles in {nice_name}",
                          self_url=f"{base}/feeds/{slugify(journal)}.xml"),
                encoding="utf-8",
            )
        print(f"Wrote {len(by_journal)} per-journal feeds to {feeds_dir}/")

    opml_path = out_path.parent / "subscriptions.opml"
    opml_path.write_text(build_opml(), encoding="utf-8")
    print(f"Wrote {opml_path}")

    if failed:
        print(f"{len(failed)} journal(s) failed: {failed}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
