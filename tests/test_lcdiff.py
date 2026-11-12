import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lcdiff import cli, har as har_mod, inference, openapi  # noqa: E402
from lcdiff import diff as diff_mod  # noqa: E402

FX = os.path.join(os.path.dirname(__file__), "fixtures")
CANARY = "CANARY-UNIQUE-7f3a9d-DO-NOT-LEAK"


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def baseline_for(name):
    entries, _ = har_mod.load_har(os.path.join(FX, name + ".har"))
    return inference.build_baseline(entries)


def findings_for(name, spec_name=None):
    baseline = baseline_for(name)
    spec = openapi.load_spec(os.path.join(FX, (spec_name or name) + ".openapi.json"))
    docs = openapi.spec_endpoints(spec)
    return diff_mod.diff_baseline_vs_spec(baseline, docs), baseline


def codes(findings):
    return [(f["code"], f["field"]) for f in findings]


class CleanFixture(unittest.TestCase):
    def test_clean_zero_findings(self):
        findings, _ = findings_for("clean")
        self.assertEqual(findings, [])

    def test_refs_allof_zero_findings(self):
        findings, _ = findings_for("refs")
        self.assertEqual(findings, [])

    def test_path_templating(self):
        _, baseline = findings_for("clean")
        self.assertIn("GET /users/{id}", baseline["endpoints"])

    def test_uuid_path_templating(self):
        self.assertEqual(
            har_mod.path_template("/a/123/b/550e8400-e29b-41d4-a716-446655440000"),
            "/a/{id}/b/{id}")


class PlantedFaults(unittest.TestCase):
    def test_required_missing_nested(self):
        findings, _ = findings_for("fault_required_nested")
        self.assertEqual(codes(findings), [("REQUIRED_MISSING", "address.zip")])
        # INTENTIONAL CHANGE (C1 sample-size guard): this fixture has n=1
        # observed response (<5), so the finding is downgraded to warning
        # with a low_sample flag instead of breaking.
        self.assertEqual(findings[0]["severity"], "warning")
        self.assertTrue(findings[0]["evidence"]["low_sample"])

    def test_required_missing_array_items(self):
        findings, _ = findings_for("fault_required_array")
        self.assertEqual(codes(findings), [("REQUIRED_MISSING", "items[].sku")])

    def test_undocumented_top_level(self):
        findings, _ = findings_for("fault_undocumented_top")
        self.assertEqual(codes(findings), [("UNDOCUMENTED_FIELD", "nickname")])
        self.assertEqual(findings[0]["severity"], "warning")

    def test_undocumented_array_items(self):
        findings, _ = findings_for("fault_undocumented_array")
        self.assertEqual(codes(findings), [("UNDOCUMENTED_FIELD", "items[].discount")])

    def test_type_mismatch_top_level(self):
        findings, _ = findings_for("fault_type_top")
        self.assertEqual(codes(findings), [("TYPE_MISMATCH", "id")])

    def test_type_mismatch_array_items(self):
        findings, _ = findings_for("fault_type_array")
        self.assertEqual(codes(findings), [("TYPE_MISMATCH", "items[].qty")])

    def test_enum_growth_top_level(self):
        findings, _ = findings_for("fault_enum_top")
        self.assertEqual(codes(findings), [("ENUM_GROWTH", "status")])
        self.assertEqual(findings[0]["evidence"]["extra_values"], ["deleted"])

    def test_enum_growth_array_items(self):
        findings, _ = findings_for("fault_enum_array")
        self.assertEqual(codes(findings), [("ENUM_GROWTH", "items[].state")])
        self.assertEqual(findings[0]["evidence"]["extra_values"], ["lost"])


class RatesAndTypes(unittest.TestCase):
    def test_sparse_field_rate(self):
        _, baseline = findings_for("sparse")
        f = baseline["endpoints"]["GET /users/{id}"]["status_groups"]["200"]["fields"]["coupon"]
        self.assertEqual(f["present_count"], 1)
        self.assertEqual(f["parent_count"], 50)
        self.assertAlmostEqual(f["presence_rate"], 0.02)

    def test_sparse_documented_optional_no_finding(self):
        findings, _ = findings_for("sparse")
        self.assertEqual(findings, [])

    def test_integer_for_number_is_ok(self):
        findings, _ = findings_for("intfloat_ok", "intfloat")
        self.assertEqual(findings, [])

    def test_float_for_integer_is_mismatch(self):
        # INTENTIONAL CHANGE (C7): the fixture's 1.0 is an INTEGRAL float,
        # which now satisfies spec integer (mainstream-validator behaviour).
        # A genuinely non-integral float (1.5) still mismatches — covered by
        # test_fixpass.py::test_nonintegral_float_for_integer_mismatch.
        findings, baseline = findings_for("intfloat_bad", "intfloat")
        self.assertEqual(findings, [])
        f = baseline["endpoints"]["GET /metrics/{id}"]["status_groups"]["200"]["fields"]["count"]
        self.assertEqual(f["types"], ["number"])
        self.assertTrue(f["all_integral"])

    def test_format_hint_email(self):
        _, baseline = findings_for("clean")
        f = baseline["endpoints"]["GET /users/{id}"]["status_groups"]["200"]["fields"]["email"]
        self.assertIn("email", f["format_hints"])

    def test_nullable_observed(self):
        text = json.dumps({"log": {"entries": [
            {"request": {"method": "GET", "url": "https://x/u/1"},
             "response": {"status": 200, "content": {"mimeType": "application/json",
                          "text": json.dumps({"id": None})}}}]}})
        entries, _ = har_mod.parse_har_text(text)
        b = inference.build_baseline(entries)
        f = b["endpoints"]["GET /u/{id}"]["status_groups"]["200"]["fields"]["id"]
        self.assertTrue(f["nullable"])
        self.assertEqual(f["types"], ["null"])


class Privacy(unittest.TestCase):
    def test_canary_not_in_baseline(self):
        baseline = baseline_for("canary")
        blob = json.dumps(baseline)
        self.assertNotIn(CANARY, blob)
        self.assertNotIn("session-05", blob)

    def test_canary_field_enum_open_and_empty(self):
        baseline = baseline_for("canary")
        f = baseline["endpoints"]["GET /users/{id}"]["status_groups"]["200"]["fields"]["session_token_hint"]
        self.assertTrue(f["enum_open"])
        self.assertEqual(f["enum_candidates"], [])

    def test_enum_cap_boundary(self):
        # 20 distinct values: candidates kept. 21: enum_open, candidates dropped.
        def make(n):
            ents = [{"request": {"method": "GET", "url": "https://x/s/%d" % i},
                     "response": {"status": 200, "content": {"mimeType": "application/json",
                                  "text": json.dumps({"s": "v%02d" % i})}}}
                    for i in range(n)]
            entries, _ = har_mod.parse_har_text(json.dumps({"log": {"entries": ents}}))
            b = inference.build_baseline(entries)
            return b["endpoints"]["GET /s/{id}"]["status_groups"]["200"]["fields"]["s"]
        self.assertEqual(len(make(20)["enum_candidates"]), 20)
        self.assertFalse(make(20)["enum_open"])
        self.assertTrue(make(21)["enum_open"])
        self.assertEqual(make(21)["enum_candidates"], [])


class EdgeBodies(unittest.TestCase):
    def test_204_empty_nonjson_do_not_crash(self):
        findings, baseline = findings_for("edge_bodies")
        self.assertEqual(findings, [])
        ep = baseline["endpoints"]["GET /users/{id}"]["status_groups"]
        # Baseline v2 (B6): 2xx groups are keyed by exact status code.
        self.assertEqual(ep["200"]["response_count"], 3)
        self.assertEqual(ep["200"]["json_response_count"], 1)
        self.assertIn("5xx", ep)  # non-2xx grouped by class, separately
        dep = baseline["endpoints"]["DELETE /users/{id}"]["status_groups"]
        self.assertIn("204", dep)  # 204 recorded as its own exact group (B6)

    def test_deeply_nested_no_crash(self):
        body = cur = {}
        for _ in range(200):
            cur["a"] = {}
            cur = cur["a"]
        cur["leaf"] = 1
        text = json.dumps({"log": {"entries": [
            {"request": {"method": "GET", "url": "https://x/deep"},
             "response": {"status": 200, "content": {"mimeType": "application/json",
                          "text": json.dumps(body)}}}]}})
        entries, _ = har_mod.parse_har_text(text)
        b = inference.build_baseline(entries)  # must not raise / recurse forever
        self.assertIn("GET /deep", b["endpoints"])

    def test_unicode_fine(self):
        text = json.dumps({"log": {"entries": [
            {"request": {"method": "GET", "url": "https://x/u/1"},
             "response": {"status": 200, "content": {"mimeType": "application/json",
                          "text": json.dumps({"name": "José 🎉"})}}}]}},
            ensure_ascii=False)
        entries, _ = har_mod.parse_har_text(text)
        b = inference.build_baseline(entries)
        self.assertIn("GET /u/{id}", b["endpoints"])


class ErrorsAndExitCodes(unittest.TestCase):
    def test_malformed_har_exit_2(self):
        code, _, err = run_cli(["observe", "--har", os.path.join(FX, "malformed.har"),
                                "--out", "/tmp/lcd-test-b.json"])
        self.assertEqual(code, 2)
        self.assertIn("error", err)

    def test_truncated_body_exit_2(self):
        code, _, _ = run_cli(["observe", "--har", os.path.join(FX, "truncated_body.har"),
                              "--out", "/tmp/lcd-test-b.json"])
        self.assertEqual(code, 2)

    def test_missing_file_exit_2(self):
        code, _, _ = run_cli(["observe", "--har", "/tmp/does-not-exist.har"])
        self.assertEqual(code, 2)

    def test_yaml_spec_loads_and_unsupported_rejected(self):
        # FLIPPED by design (YAML support, v0.2): this test used to assert
        # a YAML spec exits 2 because YAML was unsupported. Now a valid
        # YAML spec diffs exactly like its JSON twin (exit 0 on the clean
        # fixture) — see tests/test_yaml.py for the full equivalence
        # corpus — while a YAML spec using an unsupported construct
        # (anchors) still exits 2 with a clear message, and a YAML doc
        # that is not a valid OpenAPI spec still exits 2.
        import tempfile
        tmp = tempfile.mkdtemp()
        bpath = os.path.join(tmp, "b.json")
        code, _, err = run_cli(["observe", "--har", os.path.join(FX, "clean.har"),
                                "--out", bpath])
        self.assertEqual(code, 0, err)
        code, out, err = run_cli(["diff", "--baseline", bpath, "--spec",
                                  os.path.join(FX, "clean.openapi.yaml")])
        self.assertEqual(code, 0, err)
        self.assertIn("No drift", out)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("openapi: 3.0.3\ninfo: &anchor\n  title: T\npaths: {}\n")
            path = f.name
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", path])
        self.assertEqual(code, 2)
        self.assertIn("anchor", err)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("openapi: 3.0.3\npaths: {}\n")
            path = f.name
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", path])
        self.assertEqual(code, 2)
        self.assertIn("error", err)

    def test_cli_clean_exit_0_and_breaking_exit_1(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        bpath = os.path.join(tmp, "b.json")
        code, _, _ = run_cli(["observe", "--har", os.path.join(FX, "clean.har"), "--out", bpath])
        self.assertEqual(code, 0)
        code, out, _ = run_cli(["diff", "--baseline", bpath, "--spec",
                                os.path.join(FX, "clean.openapi.json")])
        self.assertEqual(code, 0)
        self.assertIn("No drift", out)
        code, _, _ = run_cli(["observe", "--har",
                              os.path.join(FX, "fault_required_nested.har"), "--out", bpath])
        self.assertEqual(code, 0)
        # INTENTIONAL CHANGE (C1): the n=1 fixture is low-sample, so its
        # REQUIRED_MISSING is a warning and the CLI exits 0. A breaking
        # exit-1 case needs >=5 samples — built inline here (6 responses,
        # required "sku" never sent).
        code, out, _ = run_cli(["diff", "--baseline", bpath, "--spec",
                                os.path.join(FX, "fault_required_nested.openapi.json")])
        self.assertEqual(code, 0)
        self.assertIn("REQUIRED_MISSING", out)
        ents = [{"request": {"method": "GET", "url": "https://x/users/%d" % i},
                 "response": {"status": 200, "content": {"mimeType": "application/json",
                              "text": json.dumps({"id": i, "name": "n",
                                                  "address": {"city": "c"}})}}}
                for i in range(6)]
        harpath = os.path.join(tmp, "six.har")
        with open(harpath, "w") as fh:
            json.dump({"log": {"entries": ents}}, fh)
        code, _, _ = run_cli(["observe", "--har", harpath, "--out", bpath])
        self.assertEqual(code, 0)
        code, out, _ = run_cli(["diff", "--baseline", bpath, "--spec",
                                os.path.join(FX, "fault_required_nested.openapi.json")])
        self.assertEqual(code, 1)
        self.assertIn("REQUIRED_MISSING", out)


class BaselineVsBaseline(unittest.TestCase):
    def test_new_field_and_enum_growth(self):
        old = baseline_for("clean")
        new = baseline_for("fault_undocumented_top")
        findings = diff_mod.diff_baselines(old, new)
        self.assertIn(("UNDOCUMENTED_FIELD", "nickname"), codes(findings))

    def test_identical_baselines_no_findings(self):
        b = baseline_for("clean")
        self.assertEqual(diff_mod.diff_baselines(b, b), [])


if __name__ == "__main__":
    unittest.main()
