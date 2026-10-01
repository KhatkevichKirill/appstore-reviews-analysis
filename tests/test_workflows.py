import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
WORKFLOWS = {
    name: os.path.join(ROOT, "n8n", name)
    for name in ("app-store-reviews-to-sheet-slack.json", "app-store-ratings-snapshot.json")
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


if __name__ == "__main__":
    unittest.main()
