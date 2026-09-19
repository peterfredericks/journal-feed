#!/usr/bin/env python3
"""Network-free tests for em_journal_feed.py. Run:  python3 -m unittest discover tests -v

Every generated feed is parsed with xml.etree.ElementTree — malformed escaping
has bitten this project before.
"""
import datetime
import email.utils
import io
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import em_journal_feed as f  # noqa: E402

NASTY_STRINGS = [
    "Plain title",
    "AT&T vs. <script>alert('x')</script> & \"quotes\" 'apos'",
    "CDATA breaker ]]> and <![CDATA[ opener",
    "Already-escaped &amp; &lt;i&gt;entities&lt;/i&gt; &#x3b1;-blocker",
    "Inline <i>markup</i> with CO<sub>2</sub> and <sup>18</sup>F",
    "p < 0.05 and n > 30 (literal comparison operators)",
    "Control chars \x00\x01\x08\x0b\x0c\x1f\x7f inside",
    "Unicode: naïve café β-blocker ≥ 5 µg — “smart quotes” 日本語 العربية 🚑",
    "Lone surrogate-free astral 𝛼𝛽𝛾 and BOM ﻿ and noncharacter ￿ ￾",
    "  leading/trailing   and    internal\t\twhitespace\n\nnewlines  ",
    "A" * 5000,
    "",
    "&&&&&& <<<<<< >>>>>> ;;;;;;",
    "<?xml version='1.0'?><!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><x>&e;</x>",
]

NASTY_DOIS = [
    "10.1016/j.annemergmed.2026.06.010",
    "10.1002/(SICI)1097-0258(19980130)17:2<1::AID-SIM1>3.0.CO;2-A",
    "10.1000/a&b=c?d#e%20f+g",
    "10.1000/spaces in doi",
    "10.1000/ünïcödé/日本",
    "10.1000/\"quoted\"'apos'",
    "",
]


def summary_record(pmid, title="T", doi="10.1/x", edat="2026/09/15 06:00",
                   sortpubdate="2026/11/01 00:00", journal="Test journal"):
    ids = [{"idtype": "pubmed", "value": str(pmid)}]
    if doi:
        ids.append({"idtype": "doi", "value": doi})
    return {
        "uid": str(pmid), "title": title, "fulljournalname": journal, "source": "Test J",
        "authors": [{"name": f"Author{i} A&B"} for i in range(9)],
        "sortpubdate": sortpubdate, "epubdate": "", "pubdate": "2026 Nov",
        "articleids": ids, "pubtype": ["Journal Article", "Randomized Controlled Trial"],
        "history": [{"pubstatus": "received", "date": "2026/01/01 00:00"},
                    {"pubstatus": "entrez", "date": edat}],
    }


def esummary_payload(records):
    result = {"uids": [r["uid"] for r in records]}
    result.update({r["uid"]: r for r in records})
    return {"result": result}


def parse(xml_text):
    root = ET.fromstring(xml_text.encode("utf-8"))
    assert root.tag == "rss"
    return root.find("channel")


def art(pmid, **kw):
    base = {"pmid": str(pmid), "title": "T", "journal": "J", "authors": "A B",
            "pubdate": "2026/09/15 06:00", "doi": "10.1/x", "pubtypes": "Journal Article"}
    base.update(kw)
    return base


class FakeResponse:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class BuildRssTests(unittest.TestCase):
    def test_hostile_strings_roundtrip_through_xml(self):
        """Any text in any field must produce well-formed XML and survive intact."""
        arts = [art(i, title=s, journal=s, authors=s, pubtypes=s)
                for i, s in enumerate(NASTY_STRINGS)]
        ch = parse(f.build_rss(arts, title=NASTY_STRINGS[1], desc=NASTY_STRINGS[2]))
        self.assertEqual(ch.findtext("title"), NASTY_STRINGS[1])
        self.assertEqual(ch.findtext("description"), NASTY_STRINGS[2])
        items = ch.findall("item")
        self.assertEqual(len(items), len(NASTY_STRINGS))
        for it, s in zip(items, NASTY_STRINGS):
            if "\x00" in s or "￿" in s:
                continue  # build_rss is only fed clean_text output; covered below
            self.assertEqual(it.findtext("title"), f"{s} — {s}")
            self.assertEqual(it.findtext("category"), s)

    def test_channel_header_not_shadowed_by_items(self):
        """Regression: item vars once overwrote the channel title/description."""
        ch = parse(f.build_rss([art(1, title="ITEM TITLE")], title="CHANNEL", desc="CHANNEL DESC"))
        self.assertEqual(ch.findtext("title"), "CHANNEL")
        self.assertEqual(ch.findtext("description"), "CHANNEL DESC")

    def test_links_use_qurl_and_decode_to_exact_target(self):
        for i, doi in enumerate(NASTY_DOIS):
            ch = parse(f.build_rss([art(100 + i, doi=doi)]))
            link = ch.find("item").findtext("link")
            self.assertTrue(link.startswith(f.PROXY_PREFIX), link)
            self.assertIn("?qurl=", f.PROXY_PREFIX)
            encoded = link[len(f.PROXY_PREFIX):]
            # target must be fully encoded: nothing that could split the query string
            for ch_ in "&#?/: <>\"'":
                self.assertNotIn(ch_, encoded)
            expected = f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{100 + i}/"
            self.assertEqual(urllib.parse.unquote(encoded), expected)
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
            self.assertEqual(qs, {"qurl": [expected]})

    def test_description_is_valid_html_with_both_links(self):
        ch = parse(f.build_rss([art(7, doi=NASTY_DOIS[1], authors="O'Brien & Sons <x>")]))
        desc = ch.find("item").findtext("description")
        # The description is HTML; it must itself be parseable once wrapped
        frag = ET.fromstring(f"<d>{desc}</d>")
        hrefs = [a.get("href") for a in frag.iter("a")]
        self.assertEqual(len(hrefs), 2)
        self.assertTrue(all(h.startswith(f.PROXY_PREFIX) for h in hrefs))
        self.assertIn("O'Brien & Sons <x>", "".join(frag.itertext()))

    def test_guid_and_pubdate(self):
        ch = parse(f.build_rss([art(42550728)]))
        it = ch.find("item")
        self.assertEqual(it.findtext("guid"), "pmid:42550728")
        self.assertEqual(it.find("guid").get("isPermaLink"), "false")
        dt = email.utils.parsedate_to_datetime(it.findtext("pubDate"))
        self.assertEqual(dt, datetime.datetime(2026, 9, 15, 6, 0, tzinfo=datetime.timezone.utc))
        email.utils.parsedate_to_datetime(ch.findtext("lastBuildDate"))

    def test_empty_feed_is_valid(self):
        ch = parse(f.build_rss([]))
        self.assertEqual(ch.findall("item"), [])
        self.assertTrue(ch.findtext("title"))

    def test_large_feed_scales(self):
        arts = [art(i, title=NASTY_STRINGS[i % len(NASTY_STRINGS)][:200] or "x") for i in range(20000)]
        arts = [dict(a, title=f.clean_text(a["title"])) for a in arts]
        t0 = time.time()
        xml_text = f.build_rss(arts)
        ch = parse(xml_text)
        self.assertEqual(len(ch.findall("item")), 20000)
        self.assertLess(time.time() - t0, 20)


class DateTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(f.parse_date("2026/09/15 06:00"), datetime.datetime(2026, 9, 15, 6, 0))
        self.assertEqual(f.parse_date("2026/09/15"), datetime.datetime(2026, 9, 15))
        self.assertEqual(f.parse_date("2025 Nov 3"), datetime.datetime(2025, 11, 3))
        self.assertEqual(f.parse_date("2025 Nov"), datetime.datetime(2025, 11, 1))
        self.assertEqual(f.parse_date("2025"), datetime.datetime(2025, 1, 1))

    def test_future_dates_clamped_to_now(self):
        far = (f.utcnow() + datetime.timedelta(days=90)).strftime("%Y/%m/%d %H:%M")
        self.assertLessEqual(f.parse_date(far), f.utcnow())

    def test_garbage_is_stable_not_now(self):
        for junk in ["", "   ", "Winter 2026", "2026 Sep-Oct", None, 12345, "9999/99/99"]:
            self.assertEqual(f.parse_date(junk), datetime.datetime(2000, 1, 1), junk)

    def test_rfc822_is_locale_independent_english(self):
        self.assertEqual(f.rfc822(datetime.datetime(2026, 9, 5, 4, 3, 2)),
                         "Sat, 05 Sep 2026 04:03:02 +0000")


class CleanTextTests(unittest.TestCase):
    def test_entities_and_tags(self):
        self.assertEqual(f.clean_text("CO&lt;sub&gt;2&lt;/sub&gt; &amp; <i>E. coli</i>"), "CO2 & E. coli")

    def test_keeps_comparison_operators(self):
        self.assertEqual(f.clean_text("p < 0.05 and n > 30"), "p < 0.05 and n > 30")
        self.assertEqual(f.clean_text("a <b or c> d"), "a <b or c> d")

    def test_strips_xml_illegal_characters(self):
        out = f.clean_text("a\x00b\x0bc￿d￾e")
        self.assertEqual(out, "abcde")
        parse(f.build_rss([art(1, title=out)]))

    def test_none_and_nonstring(self):
        self.assertEqual(f.clean_text(None), "")
        self.assertEqual(f.clean_text(42), "42")

    def test_every_nasty_string_becomes_xml_safe(self):
        for s in NASTY_STRINGS:
            cleaned = f.clean_text(s)
            ch = parse(f.build_rss([art(1, title=cleaned, journal=cleaned)]))
            self.assertEqual(ch.find("item").findtext("category"), cleaned)


class SlugTests(unittest.TestCase):
    def test_configured_journals_have_unique_sane_slugs(self):
        slugs = [f.slugify(j) for j in f.JOURNALS]
        self.assertEqual(len(set(slugs)), len(slugs), slugs)
        for s in slugs:
            self.assertRegex(s, r"^[a-z0-9]+(-[a-z0-9]+)*$")
            self.assertLessEqual(len(s), 60)

    def test_examples(self):
        self.assertEqual(f.slugify("Emergency medicine journal : EMJ"), "emergency-medicine-journal")
        self.assertEqual(f.slugify("Lancet (London, England)"), "lancet-london-england")
        self.assertEqual(f.slugify("JAMA"), "jama")
        self.assertEqual(f.slugify("!!!"), "journal")
        self.assertEqual(f.slugify("../../etc/passwd"), "etc-passwd")

    def test_jama_family_does_not_collide(self):
        names = ["JAMA", "JAMA internal medicine", "JAMA network open"]
        self.assertEqual(len({f.slugify(n) for n in names}), 3)


class FetchSummariesTests(unittest.TestCase):
    def run_with(self, payload, pmids=("1",)):
        with mock.patch.object(f, "eutils_get", return_value=payload):
            return f.fetch_summaries(list(pmids))

    def test_uses_entrez_date_not_future_issue_date(self):
        a = self.run_with(esummary_payload([summary_record(1)]))[0]
        self.assertEqual(a["pubdate"], "2026/09/15 06:00")

    def test_falls_back_when_no_history(self):
        rec = summary_record(1)
        del rec["history"]
        self.assertEqual(self.run_with(esummary_payload([rec]))[0]["pubdate"], "2026/11/01 00:00")

    def test_sparse_and_malformed_records(self):
        recs = [{"uid": "1"},
                {"uid": "2", "error": "cannot get document summary"},
                {"uid": "3", "title": None, "authors": [], "articleids": [], "pubtype": []},
                {"uid": "4", "title": "...", "fulljournalname": "", "source": "Src"}]
        arts = self.run_with(esummary_payload(recs), pmids="1234")
        self.assertEqual([a["pmid"] for a in arts], ["1", "3", "4"])
        self.assertEqual(arts[0]["title"], "[No title]")
        self.assertEqual(arts[2]["title"], "[No title]")
        self.assertEqual(arts[2]["journal"], "Src")
        parse(f.build_rss(arts))

    def test_nasty_everything_end_to_end(self):
        recs = [summary_record(i, title=s, doi=NASTY_DOIS[i % len(NASTY_DOIS)], journal=s)
                for i, s in enumerate(NASTY_STRINGS)]
        arts = self.run_with(esummary_payload(recs), pmids=[r["uid"] for r in recs])
        ch = parse(f.build_rss(arts))
        self.assertEqual(len(ch.findall("item")), len(NASTY_STRINGS))

    def test_empty_inputs(self):
        self.assertEqual(f.fetch_summaries([]), [])
        self.assertEqual(self.run_with({}), [])
        self.assertEqual(self.run_with({"result": {}}), [])

    def test_author_cap(self):
        a = self.run_with(esummary_payload([summary_record(1)]))[0]
        self.assertEqual(a["authors"].count("Author"), 6)


@mock.patch.object(f.time, "sleep", lambda s: None)
class EutilsRetryTests(unittest.TestCase):
    def http_error(self, code):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))

    def test_retries_429_then_succeeds(self):
        seq = [self.http_error(429), self.http_error(503), FakeResponse({"esearchresult": {"idlist": ["9"]}})]
        with mock.patch.object(f.urllib.request, "urlopen", side_effect=seq) as m:
            self.assertEqual(f.search_journal("X"), ["9"])
            self.assertEqual(m.call_count, 3)

    def test_rate_limit_error_inside_200_body_is_retried(self):
        seq = [FakeResponse({"error": "API rate limit exceeded", "count": "4"}),
               FakeResponse({"esearchresult": {"idlist": ["1", "2"]}})]
        with mock.patch.object(f.urllib.request, "urlopen", side_effect=seq):
            self.assertEqual(f.search_journal("X"), ["1", "2"])

    def test_esearch_backend_error_raises_not_silently_empty(self):
        body = {"esearchresult": {"ERROR": "Search Backend failed"}}
        with mock.patch.object(f.urllib.request, "urlopen", side_effect=[FakeResponse(body)] * f.MAX_RETRIES):
            with self.assertRaises(RuntimeError):
                f.search_journal("X")

    def test_gives_up_after_max_retries(self):
        with mock.patch.object(f.urllib.request, "urlopen",
                               side_effect=[urllib.error.URLError("down")] * 10) as m:
            with self.assertRaises(urllib.error.URLError):
                f.eutils_get("esearch.fcgi", {})
            self.assertEqual(m.call_count, f.MAX_RETRIES)

    def test_client_error_is_not_retried(self):
        with mock.patch.object(f.urllib.request, "urlopen", side_effect=[self.http_error(400)] * 5) as m:
            with self.assertRaises(urllib.error.HTTPError):
                f.eutils_get("esearch.fcgi", {})
            self.assertEqual(m.call_count, 1)

    def test_garbage_and_truncated_bodies(self):
        seq = [FakeResponse(b"<html>502 Bad Gateway</html>"), FakeResponse(b'{"esearchresult": {"idl'),
               FakeResponse(b'{"esearchresult": {"idlist": ["5"], "x": "raw\x0bcontrol"}}')]
        with mock.patch.object(f.urllib.request, "urlopen", side_effect=seq):
            self.assertEqual(f.search_journal("X"), ["5"])

    def test_no_api_key_sent_unless_configured(self):
        self.assertNotIn("api_key", f.TOOL_PARAMS)  # tests run without the env var
        self.assertGreaterEqual(f.REQUEST_DELAY, 0.34)  # 3 req/s ceiling without a key

    def test_query_is_well_formed(self):
        captured = {}

        def fake(req, timeout=0):
            captured["url"] = req.full_url
            return FakeResponse({"esearchresult": {"idlist": []}})

        with mock.patch.object(f.urllib.request, "urlopen", side_effect=fake):
            f.search_journal("Lancet (London, England)")
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
        self.assertEqual(qs["term"], ['"Lancet (London, England)"[Journal]'])
        self.assertEqual(qs["datetype"], ["edat"])
        self.assertEqual(qs["retmode"], ["json"])


@mock.patch.object(f.time, "sleep", lambda s: None)
class MainTests(unittest.TestCase):
    def run_main(self, out, search, summaries=None):
        def fake_fetch(pmids):
            return [art(p, journal="Nice Name", pubdate=f"2026/09/{10 + int(p) % 9:02d} 06:00") for p in pmids]

        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch.object(f, "search_journal", side_effect=search), \
                mock.patch.object(f, "fetch_summaries", side_effect=summaries or fake_fetch), \
                mock.patch.object(sys, "argv", ["x", str(out)]), \
                redirect_stdout(buf_out), redirect_stderr(buf_err):
            rc = f.main()
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def test_happy_path_writes_all_feeds_deduped_and_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "sub" / "feed.xml"   # parent dir doesn't exist yet
            # every journal returns an overlapping PMID "5" -> must dedupe in merged
            rc, _, _ = self.run_main(out, lambda j: ["5", str(f.JOURNALS.index(j) + 100)])
            self.assertEqual(rc, 0)
            ch = ET.parse(out).getroot().find("channel")
            guids = [i.findtext("guid") for i in ch.findall("item")]
            self.assertEqual(len(guids), len(set(guids)))
            self.assertEqual(len(guids), len(f.JOURNALS) + 1)
            dates = [email.utils.parsedate_to_datetime(i.findtext("pubDate")) for i in ch.findall("item")]
            self.assertEqual(dates, sorted(dates, reverse=True))
            files = sorted((out.parent / "feeds").glob("*.xml"))
            self.assertEqual(len(files), len(f.JOURNALS))
            for fp in files:
                c = ET.parse(fp).getroot().find("channel")
                self.assertEqual(c.findtext("title"), "Nice Name — JHU proxied")
                self.assertEqual(len(c.findall("item")), 2)

    def test_idempotent_across_runs(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "feed.xml"
            self.run_main(out, lambda j: ["1", "2", "3"])
            first = [(i.findtext("guid"), i.findtext("link"), i.findtext("pubDate"))
                     for i in ET.parse(out).getroot().iter("item")]
            self.run_main(out, lambda j: ["1", "2", "3"])
            second = [(i.findtext("guid"), i.findtext("link"), i.findtext("pubDate"))
                      for i in ET.parse(out).getroot().iter("item")]
            self.assertEqual(first, second)

    def test_total_outage_keeps_last_good_feeds(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "feed.xml"
            self.run_main(out, lambda j: ["1"])
            good = out.read_bytes()
            good_feed = (Path(d) / "feeds" / "jama.xml").read_bytes()

            def boom(j):
                raise urllib.error.URLError("NCBI down")

            rc, _, err = self.run_main(out, boom)
            self.assertEqual(rc, 1)
            self.assertIn("NOT rewritten", err)
            self.assertEqual(out.read_bytes(), good)
            self.assertEqual((Path(d) / "feeds" / "jama.xml").read_bytes(), good_feed)

    def test_partial_outage_preserves_failed_journals_feed(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "feed.xml"
            self.run_main(out, lambda j: ["1"])
            jama_before = (Path(d) / "feeds" / "jama.xml").read_bytes()

            def flaky(j):
                if j == "JAMA":
                    raise RuntimeError("NCBI error: backend")
                return ["2"]

            rc, _, err = self.run_main(out, flaky)
            self.assertEqual(rc, 2)
            self.assertIn("JAMA", err)
            self.assertEqual((Path(d) / "feeds" / "jama.xml").read_bytes(), jama_before)
            chest = ET.parse(Path(d) / "feeds" / "chest.xml").getroot()
            self.assertEqual([i.findtext("guid") for i in chest.iter("item")], ["pmid:2"])
            ET.parse(out)

    def test_all_journals_empty_is_success_with_valid_empty_feeds(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "feed.xml"
            rc, _, _ = self.run_main(out, lambda j: [])
            self.assertEqual(rc, 0)
            self.assertEqual(list(ET.parse(out).getroot().iter("item")), [])
            for fp in (Path(d) / "feeds").glob("*.xml"):
                ch = ET.parse(fp).getroot().find("channel")
                self.assertTrue(ch.findtext("title").endswith("— JHU proxied"))

    def test_slug_collision_is_refused(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(f, "JOURNALS", ["Chest", "chest!", "JAMA"]):
            with self.assertRaises(SystemExit):
                self.run_main(Path(d) / "feed.xml", lambda j: ["1"])


class OpmlTests(unittest.TestCase):
    def outlines(self, xml_text):
        root = ET.fromstring(xml_text.encode("utf-8"))
        self.assertEqual(root.tag, "opml")
        self.assertEqual(root.get("version"), "2.0")
        self.assertTrue(root.findtext("head/title"))
        return root.find("body")

    def test_structure_folders_and_every_journal_exactly_once(self):
        body = self.outlines(f.build_opml())
        top = list(body)
        self.assertEqual(top[0].get("xmlUrl"), f.PAGES_BASE_URL + "/feed.xml")
        folders = top[1:]
        self.assertEqual([o.get("text") for o in folders], ["Core EM", "Critical Care", "General", "Addiction"])
        urls = [o.get("xmlUrl") for fo in folders for o in fo]
        self.assertEqual(sorted(urls), sorted(f"{f.PAGES_BASE_URL}/feeds/{f.slugify(j)}.xml" for j in f.JOURNALS))
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(len(urls), 22)
        for fo in folders:
            self.assertIsNone(fo.get("xmlUrl"))
            for o in fo:
                self.assertEqual(o.get("type"), "rss")
                self.assertTrue(o.get("text") and o.get("title"))
                self.assertNotIn(" : ", o.get("text"))

    def test_urls_are_deployed_https_not_local_paths(self):
        for o in self.outlines(f.build_opml()).iter("outline"):
            if o.get("xmlUrl"):
                u = urllib.parse.urlsplit(o.get("xmlUrl"))
                self.assertEqual(u.scheme, "https")
                self.assertTrue(u.netloc.endswith("github.io"))
                self.assertRegex(u.path, r"^/journal-feed/(feed|feeds/[a-z0-9-]+)\.xml$")

    def test_groups_cover_journals_without_overlap(self):
        flat = [j for g in f.JOURNAL_GROUPS.values() for j in g]
        self.assertEqual(flat, f.JOURNALS)
        self.assertEqual(len(flat), len(set(flat)))

    def test_hostile_names_and_base_url(self):
        groups = {s or "empty": [s + " journal"] for s in NASTY_STRINGS[:10]}
        body = self.outlines(f.build_opml(groups, base_url="https://example.org/a&b/<x>/"))
        self.assertEqual(len(list(body)), 1 + len(groups))
        self.assertEqual(list(body)[0].get("xmlUrl"), "https://example.org/a&b/<x>/feed.xml")

    def test_empty_groups(self):
        self.assertEqual(len(list(self.outlines(f.build_opml({})))), 1)

    @mock.patch.object(f.time, "sleep", lambda s: None)
    def test_main_writes_opml_matching_written_feed_files(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "feed.xml"
            MainTests.run_main(self, out, lambda j: ["1"])
            body = ET.parse(Path(d) / "subscriptions.opml").getroot().find("body")
            wanted = {o.get("xmlUrl").rsplit("/", 1)[1] for o in body.iter("outline")
                      if o.get("xmlUrl") and "/feeds/" in o.get("xmlUrl")}
            written = {fp.name for fp in (Path(d) / "feeds").glob("*.xml")}
            self.assertEqual(wanted, written)


if __name__ == "__main__":
    unittest.main()
