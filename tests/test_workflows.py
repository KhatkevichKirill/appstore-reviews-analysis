import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
WORKFLOWS = {
    name: os.path.join(ROOT, "n8n", name)
    for name in (
        "app-store-reviews-to-sheet-slack.json",
        "app-store-ratings-snapshot.json",
        "app-store-ratings-weekly-report.json",
    )
}
NODE = shutil.which("node")


def load(name):
    with open(WORKFLOWS[name], encoding="utf-8") as f:
        return json.load(f)


def code_nodes(wf):
    return {n["name"]: n["parameters"]["jsCode"] for n in wf["nodes"] if n["type"] == "n8n-nodes-base.code"}


class StructureTest(unittest.TestCase):
    def test_connections_reference_existing_nodes(self):
        for name in WORKFLOWS:
            wf = load(name)
            names = [n["name"] for n in wf["nodes"]]
            self.assertEqual(len(names), len(set(names)), name)
            for source, outputs in wf["connections"].items():
                self.assertIn(source, names, name)
                for branch in outputs["main"]:
                    for target in branch:
                        self.assertIn(target["node"], names, name)

    def test_imports_inactive(self):
        for name in WORKFLOWS:
            self.assertFalse(load(name)["active"], name)


class PublicRepoHygieneTest(unittest.TestCase):
    """This repo is public: exports must not carry instance or account identifiers."""

    def test_no_credentials_or_instance_ids(self):
        for name in WORKFLOWS:
            wf = load(name)
            self.assertNotIn("meta", wf, name)
            self.assertNotIn("id", wf, name)
            for node in wf["nodes"]:
                self.assertNotIn("credentials", node, f"{name}: {node['name']}")
                self.assertNotIn("webhookId", node, f"{name}: {node['name']}")

    def test_sheet_and_channel_ids_are_placeholders(self):
        for name in WORKFLOWS:
            for node in load(name)["nodes"]:
                for key in ("documentId", "channelId"):
                    if key in node["parameters"]:
                        value = node["parameters"][key]
                        self.assertTrue(value["value"].startswith("REPLACE_WITH_"), f"{name}: {node['name']}")
                        self.assertNotIn("cachedResultUrl", value)


@unittest.skipUnless(NODE, "node is not installed")
class CodeNodeTest(unittest.TestCase):
    # n8n runs a Code node's source as a function body, with $json/$ etc. in scope.
    HARNESS = r"""
const fs = require('fs');
const [wfPath, countryBodiesPath] = process.argv.slice(2);
const wf = JSON.parse(fs.readFileSync(wfPath, 'utf8'));
const code = Object.fromEntries(
  wf.nodes.filter((n) => n.type === 'n8n-nodes-base.code').map((n) => [n.name, n.parameters.jsCode]));
const run = (src, ctx) => new Function(...Object.keys(ctx), src)(...Object.values(ctx));
const bodies = JSON.parse(fs.readFileSync(countryBodiesPath, 'utf8'));

const countries = run(code['Countries'], {});
const rows = countries.map((item) => run(code['Extract ratings'], {
  $json: { data: bodies[item.json.country] },
  $: (name) => { if (name !== 'Countries') throw new Error('unexpected node ' + name); return { item }; },
}));
console.log(JSON.stringify({ countries, rows }));
"""

    def test_all_code_nodes_parse(self):
        for name in WORKFLOWS:
            for node_name, src in code_nodes(load(name)).items():
                result = subprocess.run(
                    [NODE, "-e", f"new Function({json.dumps(src)})"], capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, f"{name}: {node_name}: {result.stderr}")

    def run_snapshot_chain(self, bodies):
        with tempfile.TemporaryDirectory() as d:
            harness = os.path.join(d, "harness.js")
            bodies_path = os.path.join(d, "bodies.json")
            with open(harness, "w") as f:
                f.write(self.HARNESS)
            with open(bodies_path, "w") as f:
                json.dump(bodies, f)
            return subprocess.run(
                [NODE, harness, WORKFLOWS["app-store-ratings-snapshot.json"], bodies_path],
                capture_output=True,
                text=True,
            )

    @staticmethod
    def lookup_body(count, average, **extra):
        app = {"trackId": 1209815023, "version": "5.5.12", "userRatingCount": count,
               "averageUserRating": average, "currentVersionReleaseDate": "2026-09-25T16:00:00Z", **extra}
        return json.dumps({"resultCount": 1, "results": [app]})

    def test_snapshot_chain_produces_sheet_rows(self):
        bodies = {
            "us": self.lookup_body(512345, 4.70923, userRatingCountForCurrentVersion=812,
                                   averageUserRatingForCurrentVersion=3.91),
            "gb": self.lookup_body(40210, 4.6),
            "ca": self.lookup_body(30111, 4.65432),
            "au": self.lookup_body(20333, 4.68),
        }
        result = self.run_snapshot_chain(bodies)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)

        self.assertEqual([c["json"]["country"] for c in out["countries"]], ["us", "gb", "ca", "au"])
        self.assertEqual(len({c["json"]["snapshotAt"] for c in out["countries"]}), 1)

        rows = [r["json"] for r in out["rows"]]
        us = rows[0]
        self.assertEqual(
            set(us),
            {"snapshot_at", "country", "app_id", "app_version", "current_version_release_date",
             "user_rating_count", "average_user_rating", "user_rating_count_current_version",
             "average_user_rating_current_version"},
        )
        self.assertEqual(us["country"], "US")
        self.assertEqual(us["user_rating_count"], 512345)
        self.assertEqual(us["average_user_rating"], "4.70923")  # text, so Sheets can't round it
        self.assertEqual(us["user_rating_count_current_version"], 812)
        self.assertEqual(rows[1]["user_rating_count_current_version"], "")  # absent field stays empty

    def test_snapshot_chain_shortens_apples_double_expansion(self):
        # Apple prints the average's full binary expansion (US response, 2026-10-01).
        raw = ('{"resultCount":1,"results":[{"trackId":1209815023,"version":"5.5.13",'
               '"userRatingCount":525034,'
               '"averageUserRating":4.6628699999999998482280716416426002979278564453125}]}')
        result = self.run_snapshot_chain({"us": raw, "gb": raw, "ca": raw, "au": raw})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["rows"][0]["json"]["average_user_rating"], "4.66287")

    def test_snapshot_chain_fails_loudly_when_app_missing(self):
        empty = json.dumps({"resultCount": 0, "results": []})
        result = self.run_snapshot_chain({"us": empty, "gb": empty, "ca": empty, "au": empty})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app 1209815023 missing", result.stderr)

    def test_snapshot_chain_fails_loudly_on_non_json(self):
        html = "<html>rate limited</html>"
        result = self.run_snapshot_chain({"us": html, "gb": html, "ca": html, "au": html})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("response is not JSON", result.stderr)


@unittest.skipUnless(NODE, "node is not installed")
class WeeklyReportTest(unittest.TestCase):
    """The weekly report's JS must agree with scripts/ratings_report.py, which carries the unit tests."""

    HARNESS = r"""
const fs = require('fs');
const [wfPath, itemsPath] = process.argv.slice(2);
const wf = JSON.parse(fs.readFileSync(wfPath, 'utf8'));
const code = wf.nodes.find((n) => n.name === 'Build weekly report').parameters.jsCode;
const items = JSON.parse(fs.readFileSync(itemsPath, 'utf8'));
const $ = (name) => ({ all: () => items[name], first: () => items[name][0] });
console.log(JSON.stringify(new Function('$', code)($)));
"""

    def run_report(self, snapshot_rows, review_rows, now):
        items = {
            "Read snapshots": [{"json": r} for r in snapshot_rows],
            "Read reviews": [{"json": r} for r in review_rows],
            "Schedule Trigger": [{"json": {"timestamp": now}}],
        }
        with tempfile.TemporaryDirectory() as d:
            harness, items_path = os.path.join(d, "h.js"), os.path.join(d, "items.json")
            with open(harness, "w") as f:
                f.write(self.HARNESS)
            with open(items_path, "w") as f:
                json.dump(items, f)
            result = subprocess.run(
                [NODE, harness, WORKFLOWS["app-store-ratings-weekly-report.json"], items_path],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)[0]["json"]

    COUNTRIES = (("US", 525000, 90, 466287), ("GB", 42100, 6, 456911),
                 ("CA", 42900, 9, 454502), ("AU", 18420, 3, 452860))

    @classmethod
    def synthetic(cls, countries=None):
        """Hourly snapshots from a Wednesday, every third answer a day-stale cache."""
        from datetime import datetime, timedelta, timezone

        start = datetime(2026, 9, 30, 16, 37, tzinfo=timezone.utc)
        rows = []
        for h in range(24 * 19):
            t = start + timedelta(hours=h)
            for country, base, per_day, avg0 in countries or cls.COUNTRIES:
                def at(hours):
                    n = base + per_day * (max(hours, 0) // 8) // 3  # Apple updates every ~8 h
                    a = (base * avg0 + 4.3 * 100000 * (n - base)) / n  # new ratings average 4.3
                    return n, round(a) / 100000
                n, a = at(h - 24 if h % 3 == 2 else h)
                rows.append({"snapshot_at": t.isoformat().replace("+00:00", "Z"), "country": country,
                             "user_rating_count": n, "average_user_rating": f"{a:.5f}".rstrip("0")})
        reviews = [
            {"date": "2026-10-06T10:00:00-07:00", "country": "US", "score": 1},
            {"date": "2026-10-07T00:00:00.000Z", "country": "US", "score": 4},
            {"date": "2026-10-08T12:00:00-07:00", "country": "GB", "score": 2},
            {"date": "2026-10-13T12:00:00-07:00", "country": "US", "score": 5},  # following week
        ]
        return rows, reviews

    def test_agrees_with_python_report(self):
        import csv as _csv
        import io as _io
        import sys as _sys
        from datetime import datetime, timezone
        _sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import ratings_report as rr

        rows, reviews = self.synthetic()
        out = self.run_report(rows, reviews, "2026-10-12T12:17:00.000-03:00")
        report = out["report"]
        self.assertEqual(report["weekStart"], datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp() * 1000)

        buf = _io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
        snaps = rr.load_snapshots(_csv.DictReader(_io.StringIO(buf.getvalue())))
        py = {s.country: s for s in rr.compute(snaps, rr.load_reviews(reviews), period="week")
              if s.start.strftime("%Y-%m-%d") == "2026-10-05"}

        js = {r["country"].upper(): r for r in report["current"]}
        for country in ("US", "GB", "CA", "AU", rr.ALL):
            self.assertEqual(js[country]["newRatings"], py[country].new_ratings, country)
            self.assertAlmostEqual(js[country]["mean"], float(py[country].new_average), places=9, msg=country)
            self.assertAlmostEqual(js[country]["err"], float(py[country].new_average_error), places=9, msg=country)
            self.assertEqual(js[country]["reviews"]["n"], py[country].reviews, country)
        self.assertGreater(js["US"]["newRatings"], 0)
        self.assertAlmostEqual(js["US"]["mean"], 4.3, delta=js["US"]["err"] + 0.01)

    def test_message_shape(self):
        rows, reviews = self.synthetic()
        out = self.run_report(rows, reviews, "2026-10-12T12:17:00.000-03:00")
        blocks = out["slackBody"]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["header", "section", "context"])
        self.assertEqual(blocks[0]["text"]["text"], "Speechify iOS · App Store ratings · Oct 5 – Oct 11")
        table = blocks[1]["text"]["text"]
        self.assertTrue(table.startswith("```\n") and table.endswith("\n```"))
        lines = table.strip("`\n").split("\n")
        self.assertEqual([l.split()[0] for l in lines[1:]], ["US", "GB", "CA", "AU", "All"])
        self.assertIn("2 (2.5★)", lines[1])
        self.assertNotIn("†", table)  # a fully covered week
        self.assertIn("new ratings", out["slackBody"]["text"])

    def test_missing_storefront_shows_no_data_and_no_total(self):
        rows, reviews = self.synthetic(countries=self.COUNTRIES[:2])
        out = self.run_report(rows, reviews, "2026-10-12T12:17:00.000-03:00")
        lines = out["slackBody"]["blocks"][1]["text"]["text"].strip("`\n").split("\n")
        self.assertIn("no data", lines[3])  # CA
        self.assertIn("no data", lines[5])  # All: a total over 4 storefronts needs all 4
        self.assertTrue(out["slackBody"]["text"].endswith("no data"))

    def test_first_partial_week_is_marked(self):
        rows, reviews = self.synthetic()
        out = self.run_report(rows, reviews, "2026-10-05T12:17:00Z")
        self.assertIn("†", out["slackBody"]["blocks"][1]["text"]["text"])
        self.assertIn("Partial week: ratings counted from Sep 30 16:37 UTC", out["slackBody"]["blocks"][2]["elements"][0]["text"])


@unittest.skipUnless(NODE, "node is not installed")
class ReviewFeedTest(unittest.TestCase):
    def test_app_store_button_opens_the_reviews_storefront(self):
        wf = load("app-store-reviews-to-sheet-slack.json")
        code = code_nodes(wf)["Build Slack payload"]
        harness = (
            "const code = process.argv[1];"
            "const items = [{json: {country: 'GB', score: 1, title: 't', text: 'b', url: 'x'}},"
            "               {json: {country: '', score: 5, title: 't', text: 'b', url: 'x'}}];"
            "const out = new Function('$input', code)({ all: () => items });"
            "console.log(JSON.stringify(out.map((o) => o.json.slackBody.blocks.at(-1).elements[0].url)));"
        )
        result = subprocess.run([NODE, "-e", harness, code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            "https://apps.apple.com/gb/app/id1209815023?see-all=reviews",
            "https://apps.apple.com/us/app/id1209815023?see-all=reviews",
        ])


if __name__ == "__main__":
    unittest.main()
