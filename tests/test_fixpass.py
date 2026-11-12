"""Regression tests for the adversarial-review fix pass (clusters A-E).

Each test names the finding it pins. Helpers build HAR/spec material
inline so every fix is proven in isolation.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lcdiff import cli, har as har_mod, inference, openapi  # noqa: E402
from lcdiff import diff as diff_mod  # noqa: E402
from lcdiff.har import IncompleteInputError  # noqa: E402


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def har_text(specs):
    """specs: list of (method, url, status, body) where body is an object,
    or a raw string, or (raw_string, mime, encoding)."""
    entries = []
    for method, url, status, body in specs:
        mime, enc, text = "application/json", None, body
        if isinstance(body, tuple):
            text, mime, enc = body
        elif not isinstance(body, str):
            text = json.dumps(body)
        content = {"mimeType": mime, "text": text}
        if enc:
            content["encoding"] = enc
        entries.append({"request": {"method": method, "url": url},
                        "response": {"status": status, "content": content}})
    return json.dumps({"log": {"entries": entries}})


def baseline_from(specs):
    entries, _ = har_mod.parse_har_text(har_text(specs))
    return inference.build_baseline(entries)


def repeat(body, n, url="https://x/u/1", status=200, method="GET"):
    return [(method, url, status, body)] * n


def spec_docs(paths, version="3.0.3", components=None):
    spec = {"openapi": version, "info": {"title": "t", "version": "1"},
            "paths": paths}
    if components:
        spec["components"] = components
    return openapi.spec_endpoints(spec)


def op(schema, status="200", method="get"):
    return {method: {"responses": {status: {"content": {
        "application/json": {"schema": schema}}}}}}


def obj_schema(props, required=(), **kw):
    s = {"type": "object", "properties": props}
    if required:
        s["required"] = list(required)
    s.update(kw)
    return s


def fields_of(baseline, ep_key, group="200"):
    return baseline["endpoints"][ep_key]["status_groups"][group]["fields"]


# ---------------------------------------------------------------- A. Privacy

class TestA1EnumPrivacy(unittest.TestCase):
    def test_long_string_candidate_not_stored(self):
        long_val = "L" * 100
        specs = repeat({"note": "short"}, 3) + [("GET", "https://x/u/1", 200, {"note": long_val})]
        b = baseline_from(specs)
        f = fields_of(b, "GET /u/{id}")["note"]
        self.assertEqual(f["enum_candidates"], ["short"])
        self.assertNotIn(long_val, json.dumps(b))

    def test_long_values_still_count_toward_cardinality(self):
        specs = [("GET", "https://x/u/1", 200, {"note": "v%02d" % i + "x" * 80})
                 for i in range(21)]
        f = fields_of(baseline_from(specs), "GET /u/{id}")["note"]
        self.assertTrue(f["enum_open"])
        self.assertEqual(f["enum_candidates"], [])

    def test_sensitive_fields_never_stored(self):
        specs = repeat({"password": "pw1", "api_token": "tok1",
                        "session_id": "s1", "status": "active"}, 3)
        specs += repeat({"password": "pw2", "api_token": "tok2",
                         "session_id": "s2", "status": "active"}, 3)
        b = baseline_from(specs)
        fs = fields_of(b, "GET /u/{id}")
        for name in ("password", "api_token", "session_id"):
            self.assertEqual(fs[name]["enum_candidates"], [], name)
            self.assertFalse(fs[name]["enum_open"], name)  # counts still recorded
            self.assertEqual(fs[name]["type_counts"], {"string": 6}, name)
        self.assertEqual(fs["status"]["enum_candidates"], ["active"])
        blob = json.dumps(b)
        for secret in ("pw1", "tok1", "s1"):
            self.assertNotIn(secret, blob)

    def test_huge_value_not_copied(self):
        huge = "H" * 200000
        b = baseline_from([("GET", "https://x/u/1", 200, {"blob": huge})])
        self.assertNotIn(huge, json.dumps(b))
        self.assertLess(len(json.dumps(b)), 100000)


class TestA2CrossGroupCap(unittest.TestCase):
    def test_union_across_status_groups_drops_candidates(self):
        specs = [("GET", "https://x/c/1", 200, {"color": "a%02d" % i}) for i in range(15)]
        specs += [("GET", "https://x/c/1", 201, {"color": "b%02d" % i}) for i in range(15)]
        b = baseline_from(specs)
        groups = b["endpoints"]["GET /c/{id}"]["status_groups"]
        self.assertIn("200", groups)
        self.assertIn("201", groups)
        for g in ("200", "201"):  # 15 each, 30 in union -> dropped in ALL
            self.assertEqual(groups[g]["fields"]["color"]["enum_candidates"], [])
            self.assertTrue(groups[g]["fields"]["color"]["enum_open"])


class TestA3MapKeyCap(unittest.TestCase):
    def test_map_keys_capped_and_no_per_key_fields(self):
        body = {"balances": {"key%02d" % i: i for i in range(60)}}
        b = baseline_from([("GET", "https://x/u/1", 200, body)])
        fs = fields_of(b, "GET /u/{id}")
        self.assertTrue(fs["balances"]["map_keys_open"])
        self.assertEqual(fs["balances"]["map_key_count"], 60)
        self.assertFalse(any(k.startswith("balances.") for k in fs))
        self.assertNotIn("key00", json.dumps(b))

    def test_map_under_cap_keeps_fields(self):
        body = {"balances": {"key%02d" % i: i for i in range(10)}}
        b = baseline_from([("GET", "https://x/u/1", 200, body)])
        fs = fields_of(b, "GET /u/{id}")
        self.assertFalse(fs["balances"]["map_keys_open"])
        self.assertIn("balances.key00", fs)

    def test_map_produces_no_per_key_diff_findings(self):
        body = {"balances": {"key%02d" % i: i for i in range(60)}}
        b = baseline_from(repeat(body, 6))
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"balances": {"type": "object"}}))})
        findings = diff_mod.diff_baseline_vs_spec(b, docs)
        self.assertFalse(any(f["field"].startswith("balances.") for f in findings))


class TestA4PathTemplating(unittest.TestCase):
    def test_token_segment_collapsed(self):
        self.assertEqual(
            har_mod.path_template("/reset/abc123def456ghi789jkl012"),
            "/reset/{id}")

    def test_decimal_and_undashed_hex_collapsed(self):
        self.assertEqual(har_mod.path_template("/v/1.5/x"), "/v/{id}/x")
        self.assertEqual(
            har_mod.path_template("/f/0123456789abcdef0123456789abcdef"),
            "/f/{id}")

    def test_year_segment_not_collapsed(self):
        self.assertEqual(har_mod.path_template("/archive/2024/posts"),
                         "/archive/2024/posts")
        self.assertEqual(har_mod.path_template("/archive/1999/posts"),
                         "/archive/1999/posts")

    def test_slug_not_collapsed(self):
        self.assertEqual(har_mod.path_template("/articles/my-post"),
                         "/articles/my-post")
        self.assertEqual(har_mod.path_template("/a/a-very-long-article-slug"),
                         "/a/a-very-long-article-slug")


class TestA5ErrorMessagesTemplated(unittest.TestCase):
    TOKEN = "abc123def456ghi789jkl012"

    def test_incomplete_error_uses_templated_path(self):
        text = har_text([("GET", f"https://x/reset/{self.TOKEN}", 200,
                          ('{"a": ', "application/json", None))])
        with self.assertRaises(IncompleteInputError) as cm:
            har_mod.parse_har_text(text)
        msg = str(cm.exception)
        self.assertNotIn(self.TOKEN, msg)
        self.assertIn("/reset/{id}", msg)

    def test_canary_absent_from_stderr_and_diff_output(self):
        canary = "CANARY-DIFF-9z8y7x-DO-NOT-LEAK"
        specs = [("GET", "https://x/u/1", 200, {"hint": "v%02d" % i})
                 for i in range(29)]
        specs.append(("GET", "https://x/u/1", 200, {"hint": canary}))
        b = baseline_from(specs)
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"hint": {"type": "string", "enum": ["a", "b"]}}))})
        report = diff_mod.diff_report(b, docs)
        blob = json.dumps(report) + diff_mod.format_text(report["findings"])
        self.assertNotIn(canary, blob)
        self.assertNotIn(canary, json.dumps(b))


# ------------------------------------------------------------- B. Correctness

class TestB1EnumKeys(unittest.TestCase):
    def test_bool_int_collision_counts_distinctly(self):
        vals = [True, 1] + list(range(2, 22))  # 22 distinct JSON values
        specs = [("GET", "https://x/u/1", 200, {"flag": v}) for v in vals]
        f = fields_of(baseline_from(specs), "GET /u/{id}")["flag"]
        self.assertTrue(f["enum_open"])  # old dict-key counting said 20/21
        self.assertEqual(f["enum_candidates"], [])

    def test_candidate_ordering_deterministic(self):
        specs = [("GET", "https://x/u/1", 200, {"m": v})
                 for v in (2, "b", 1, "a", True)]
        f = fields_of(baseline_from(specs), "GET /u/{id}")["m"]
        self.assertEqual(f["enum_candidates"], [True, 1, 2, "a", "b"])

    def test_float_enum_candidates_and_growth(self):
        specs = repeat({"p": 1.5}, 3) + repeat({"p": 2.5}, 3)
        b = baseline_from(specs)
        f = fields_of(b, "GET /u/{id}")["p"]
        self.assertEqual(f["enum_candidates"], [1.5, 2.5])
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"p": {"type": "number", "enum": [1.5]}}))})
        findings = diff_mod.diff_baseline_vs_spec(b, docs)
        self.assertEqual([(x["code"], x["field"]) for x in findings],
                         [("ENUM_GROWTH", "p")])
        self.assertEqual(findings[0]["evidence"]["extra_values"], [2.5])


class TestB2MixedArrayPresence(unittest.TestCase):
    def test_non_object_items_count_in_denominator(self):
        body = {"items": [{"sku": "a"}, 42, "text"]}
        b = baseline_from([("GET", "https://x/u/1", 200, body)])
        f = fields_of(b, "GET /u/{id}")["items[].sku"]
        self.assertEqual(f["present_count"], 1)
        self.assertEqual(f["parent_count"], 3)
        self.assertAlmostEqual(f["presence_rate"], 1 / 3, places=5)


class TestB3BranchingRefs(unittest.TestCase):
    def _branching_spec(self, nprops):
        props = {f"child{i}": {"$ref": "#/components/schemas/Node"}
                 for i in range(nprops)}
        props["val"] = {"type": "integer"}
        return spec_docs(
            {"/n": op({"$ref": "#/components/schemas/Node"})},
            components={"schemas": {"Node": {"type": "object", "properties": props}}})

    def test_branching_recursive_refs_resolve_fast(self):
        for n in (2, 3, 4):
            start = time.time()
            docs = self._branching_spec(n)
            elapsed = time.time() - start
            self.assertLess(elapsed, 1.0, f"{n} self-properties took {elapsed}s")
            self.assertIn(("GET", "/n"), docs)


class TestB4EmptyArray(unittest.TestCase):
    def test_empty_array_required_field_warns_unverifiable(self):
        b = baseline_from(repeat({"items": []}, 3))
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"items": {"type": "array", "items": obj_schema(
                {"sku": {"type": "string"}}, required=("sku",))}}))})
        findings = diff_mod.diff_baseline_vs_spec(b, docs)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["code"], "REQUIRED_MISSING")
        self.assertEqual(f["field"], "items[].sku")
        self.assertEqual(f["severity"], "warning")
        self.assertEqual(f["evidence"]["arrays_observed"], 3)
        self.assertEqual(f["evidence"]["items_observed"], 0)
        self.assertIn("unverifiable-because-empty", f["evidence"]["note"])


class TestB5NotObserved(unittest.TestCase):
    def test_zero_traffic_endpoint_not_diffed(self):
        b = baseline_from(repeat({"id": 1}, 6))
        docs = spec_docs({
            "/u/{id}": op(obj_schema({"id": {"type": "integer"}}, required=("id",))),
            "/never": op(obj_schema({"x": {"type": "string"}}, required=("x",)))})
        report = diff_mod.diff_report(b, docs)
        self.assertFalse(any(f["code"] == "REQUIRED_MISSING"
                             for f in report["findings"]))
        self.assertTrue(any(n["endpoint"] == "GET /never"
                            for n in report["not_observed"]))


class TestB6PerStatusDiff(unittest.TestCase):
    def _docs(self):
        def resp(schema):
            return {"content": {"application/json": {"schema": schema}}}
        return spec_docs({"/u/{id}": {"get": {"responses": {
            "200": resp(obj_schema({"a": {"type": "string"}}, required=("a",))),
            "201": resp(obj_schema({"b": {"type": "string"}}, required=("b",)))}}}})

    def test_each_status_diffed_against_its_own_schema(self):
        specs = repeat({"a": "x"}, 6, status=200) + repeat({"b": "y"}, 6, status=201)
        b = baseline_from(specs)
        self.assertEqual(diff_mod.diff_baseline_vs_spec(b, self._docs()), [])

    def test_undocumented_status_fields_surface_once(self):
        specs = repeat({"a": "x"}, 6, status=200) + repeat({"b": "y"}, 6, status=201)
        specs += repeat({"c": 1}, 2, status=202)
        findings = diff_mod.diff_baseline_vs_spec(baseline_from(specs), self._docs())
        undoc = [f for f in findings if f["code"] == "UNDOCUMENTED_FIELD"]
        self.assertEqual(len(undoc), 1)
        self.assertEqual(undoc[0]["field"], "c")
        self.assertEqual(undoc[0]["evidence"]["status"], "202")
        self.assertEqual(undoc[0]["severity"], "warning")


class TestB7ParentCount(unittest.TestCase):
    def test_never_observed_nested_uses_parent_instances(self):
        specs = repeat({"address": {"city": "c"}}, 2) + repeat({"id": 1}, 4)
        b = baseline_from(specs)
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"address": obj_schema({"city": {"type": "string"},
                                    "zip": {"type": "string"}},
                                   required=("zip",))}))})
        findings = diff_mod.diff_baseline_vs_spec(b, docs)
        req = [f for f in findings if f["code"] == "REQUIRED_MISSING"]
        self.assertEqual(len(req), 1)
        self.assertEqual(req[0]["field"], "address.zip")
        self.assertEqual(req[0]["evidence"]["parent_count"], 2)  # not 6


class TestB8EnumOpenThreshold(unittest.TestCase):
    def _baseline_open(self):
        return baseline_from([("GET", "https://x/u/1", 200, {"s": "v%02d" % i})
                              for i in range(25)])

    def test_enum_open_no_fire_when_spec_enum_large(self):
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"s": {"type": "string", "enum": ["e%02d" % i for i in range(25)]}}))})
        findings = diff_mod.diff_baseline_vs_spec(self._baseline_open(), docs)
        self.assertFalse(any(f["code"] == "ENUM_GROWTH" for f in findings))

    def test_enum_open_fires_when_spec_enum_small(self):
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"s": {"type": "string", "enum": ["a", "b", "c"]}}))})
        findings = diff_mod.diff_baseline_vs_spec(self._baseline_open(), docs)
        self.assertTrue(any(f["code"] == "ENUM_GROWTH" for f in findings))


# ---------------------------------------------------------- C. False positives

class TestC1SampleGuard(unittest.TestCase):
    def test_low_sample_downgrades_and_flags(self):
        b = baseline_from(repeat({"id": 1}, 3))
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"id": {"type": "integer"}, "sku": {"type": "string"}},
            required=("id", "sku")))})
        report = diff_mod.diff_report(b, docs)
        self.assertEqual(len(report["findings"]), 1)
        f = report["findings"][0]
        self.assertEqual(f["severity"], "warning")
        self.assertTrue(f["evidence"]["low_sample"])
        self.assertEqual(f["evidence"]["sample_size"], 3)
        self.assertEqual(report["endpoint_samples"], {"GET /u/{id}": {"200": 3}})

    def test_required_threshold_option(self):
        specs = repeat({"sku": "s"}, 97) + repeat({}, 3)
        b = baseline_from(specs)
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"sku": {"type": "string"}}, required=("sku",)))})
        self.assertTrue(diff_mod.diff_baseline_vs_spec(b, docs))  # 0.97 < 0.99
        self.assertEqual(
            diff_mod.diff_baseline_vs_spec(b, docs, threshold=0.95), [])

    def test_threshold_cli_validation(self):
        with self.assertRaises(SystemExit) as cm:
            run_cli(["diff", "--baseline", "x", "--spec", "y",
                     "--required-threshold", "1.5"])
        self.assertEqual(cm.exception.code, 2)


class TestC2SlugAggregation(unittest.TestCase):
    def _docs(self):
        return spec_docs({"/articles/{slug}": op(obj_schema(
            {"title": {"type": "string"}}, required=("title",)))})

    def test_slug_fragments_merge_no_phantoms(self):
        specs = [("GET", "https://x/articles/first-post", 200, {"title": "a"})] * 3
        specs += [("GET", "https://x/articles/second-post", 200, {"title": "b"})] * 3
        report = diff_mod.diff_report(baseline_from(specs), self._docs())
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["not_observed"], [])
        self.assertEqual(report["endpoint_samples"],
                         {"GET /articles/{id}": {"200": 6}})

    def test_slug_fragments_merge_single_finding(self):
        specs = [("GET", "https://x/articles/first-post", 200, {"id": 1})] * 3
        specs += [("GET", "https://x/articles/second-post", 200, {"id": 2})] * 3
        findings = diff_mod.diff_baseline_vs_spec(baseline_from(specs), self._docs())
        req = [f for f in findings if f["code"] == "REQUIRED_MISSING"]
        self.assertEqual(len(req), 1)
        self.assertEqual(req[0]["evidence"]["parent_count"], 6)


class TestC3AdditionalProperties(unittest.TestCase):
    def test_ap_schema_no_per_key_findings_and_type_check(self):
        schema = obj_schema({"rates": {"type": "object",
                                       "additionalProperties": {"type": "string"}}})
        docs = spec_docs({"/u/{id}": op(schema)})
        good = baseline_from(repeat({"rates": {"USD": "1.0", "EUR": "0.9"}}, 6))
        self.assertEqual(diff_mod.diff_baseline_vs_spec(good, docs), [])
        bad = baseline_from(repeat({"rates": {"USD": 5}}, 6))
        findings = diff_mod.diff_baseline_vs_spec(bad, docs)
        self.assertEqual([(f["code"], f["field"]) for f in findings],
                         [("TYPE_MISMATCH", "rates.USD")])

    def test_ap_true_no_per_key_findings(self):
        schema = obj_schema({"rates": {"type": "object", "additionalProperties": True}})
        docs = spec_docs({"/u/{id}": op(schema)})
        b = baseline_from(repeat({"rates": {"USD": 1, "EUR": 2, "JPY": 3}}, 6))
        self.assertEqual(diff_mod.diff_baseline_vs_spec(b, docs), [])


class TestC4FreeFormObjects(unittest.TestCase):
    def test_free_form_object_no_per_key_findings(self):
        docs = spec_docs({"/u/{id}": op(obj_schema({"meta": {"type": "object"}}))})
        b = baseline_from(repeat({"meta": {"a": 1, "b": "x"}}, 6))
        self.assertEqual(diff_mod.diff_baseline_vs_spec(b, docs), [])

    def test_object_with_properties_still_flags_extra_keys(self):
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"meta": obj_schema({"a": {"type": "integer"}})}))})
        b = baseline_from(repeat({"meta": {"a": 1, "b": 2}}, 6))
        findings = diff_mod.diff_baseline_vs_spec(b, docs)
        self.assertEqual([(f["code"], f["field"]) for f in findings],
                         [("UNDOCUMENTED_FIELD", "meta.b")])


class TestC5Variants(unittest.TestCase):
    def test_oneof_union_no_undocumented(self):
        schema = {"oneOf": [
            obj_schema({"a": {"type": "string"}}, required=("a",)),
            obj_schema({"b": {"type": "integer"}}, required=("b",))]}
        docs = spec_docs({"/u/{id}": op(schema)})
        specs = repeat({"a": "x"}, 3) + repeat({"b": 2}, 3)
        self.assertEqual(
            diff_mod.diff_baseline_vs_spec(baseline_from(specs), docs), [])

    def test_oneof_required_in_all_variants(self):
        schema = {"oneOf": [
            obj_schema({"id": {"type": "integer"}, "a": {"type": "string"}},
                       required=("id", "a")),
            obj_schema({"id": {"type": "integer"}, "b": {"type": "string"}},
                       required=("id", "b"))]}
        docs = spec_docs({"/u/{id}": op(schema)})
        b = baseline_from(repeat({"a": "x"}, 6))
        findings = diff_mod.diff_baseline_vs_spec(b, docs)
        self.assertEqual([(f["code"], f["field"]) for f in findings],
                         [("REQUIRED_MISSING", "id")])


class TestC6UndocumentedEndpoints(unittest.TestCase):
    def _material(self):
        specs = repeat({"id": 1}, 6)
        specs += [("GET", "https://x/health", 200, {"status": "ok", "uptime": 5})] * 6
        specs += [("GET", "https://x/metrics", 200, {"cpu": 1})] * 6
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"id": {"type": "integer"}}, required=("id",)))})
        return baseline_from(specs), docs

    def test_wholly_undocumented_endpoint_collapses_to_one(self):
        b, docs = self._material()
        findings = diff_mod.diff_baseline_vs_spec(b, docs)
        health = [f for f in findings if f["endpoint"] == "GET /health"]
        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["severity"], "warning")
        self.assertEqual(sorted(health[0]["evidence"]["fields"]),
                         ["status", "uptime"])

    def test_ignore_endpoint_glob(self):
        b, docs = self._material()
        findings = diff_mod.diff_baseline_vs_spec(
            b, docs, ignore_endpoints=["GET /health", "*/metrics"])
        self.assertEqual(findings, [])


class TestC7IntegralFloats(unittest.TestCase):
    def test_nonintegral_float_for_integer_mismatch(self):
        b = baseline_from(repeat({"count": 1.5}, 6))
        docs = spec_docs({"/u/{id}": op(obj_schema({"count": {"type": "integer"}}))})
        findings = diff_mod.diff_baseline_vs_spec(b, docs)
        self.assertEqual([(f["code"], f["field"]) for f in findings],
                         [("TYPE_MISMATCH", "count")])

    def test_mixed_integral_and_nonintegral_mismatch(self):
        specs = repeat({"count": 1.0}, 3) + repeat({"count": 2.5}, 3)
        b = baseline_from(specs)
        self.assertFalse(fields_of(b, "GET /u/{id}")["count"]["all_integral"])
        docs = spec_docs({"/u/{id}": op(obj_schema({"count": {"type": "integer"}}))})
        findings = diff_mod.diff_baseline_vs_spec(b, docs)
        self.assertEqual([(f["code"], f["field"]) for f in findings],
                         [("TYPE_MISMATCH", "count")])


class TestC8MismatchPrevalence(unittest.TestCase):
    def _docs(self):
        return spec_docs({"/u/{id}": op(obj_schema({"qty": {"type": "integer"}}))})

    def test_single_bad_instance_downgraded(self):
        specs = repeat({"qty": 1}, 99) + [("GET", "https://x/u/1", 200, {"qty": "one"})]
        findings = diff_mod.diff_baseline_vs_spec(baseline_from(specs), self._docs())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warning")
        self.assertEqual(findings[0]["evidence"]["mismatched_count"], 1)
        self.assertEqual(findings[0]["evidence"]["total_observations"], 100)

    def test_all_bad_instances_stay_breaking(self):
        specs = repeat({"qty": "one"}, 100)
        findings = diff_mod.diff_baseline_vs_spec(baseline_from(specs), self._docs())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "breaking")


# ------------------------------------------------------------- D. Robustness

class TestD1ErrorDiscipline(unittest.TestCase):
    def _write(self, tmp, name, obj_or_text):
        p = os.path.join(tmp, name)
        with open(p, "w") as f:
            f.write(obj_or_text if isinstance(obj_or_text, str)
                    else json.dumps(obj_or_text))
        return p

    def test_malformed_baselines_exit_2_no_traceback(self):
        tmp = tempfile.mkdtemp()
        spec_p = self._write(tmp, "spec.json", {
            "openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {"/u/{id}": op(obj_schema({"id": {"type": "integer"}}))}})
        good_field = {"present_count": 1, "parent_count": 1, "presence_rate": 1.0,
                      "types": ["integer"], "type_counts": {"integer": 1},
                      "nullable": False, "enum_candidates": [], "enum_open": False,
                      "format_hints": []}
        cases = {
            "endpoints_list": {"version": 2, "endpoints": []},
            "field_missing_keys": {"version": 2, "endpoints": {"GET /u/{id}": {
                "method": "GET", "path_template": "/u/{id}", "status_groups": {
                    "200": {"response_count": 1, "json_response_count": 1,
                            "fields": {"id": {"present_count": 1}}}}}}},
            "old_version": {"version": 1, "endpoints": {}},
            "group_wrong_type": {"version": 2, "endpoints": {"GET /u/{id}": {
                "method": "GET", "path_template": "/u/{id}",
                "status_groups": "2xx"}}},
            "field_ok_control": {"version": 2, "endpoints": {"GET /u/{id}": {
                "method": "GET", "path_template": "/u/{id}", "status_groups": {
                    "200": {"response_count": 1, "json_response_count": 1,
                            "fields": {"id": good_field}}}}}},
        }
        for name, obj in cases.items():
            bp = self._write(tmp, name + ".json", obj)
            code, out, err = run_cli(["diff", "--baseline", bp, "--spec", spec_p])
            self.assertNotIn("Traceback", err, name)
            if name == "field_ok_control":
                self.assertEqual(code, 0, (name, err))  # valid baseline works
            else:
                self.assertEqual(code, 2, (name, out, err))
                self.assertIn("error:", err, name)
        # The old-version message must say how to fix it.
        bp = self._write(tmp, "old2.json", cases["old_version"])
        _c, _o, err = run_cli(["diff", "--baseline", bp, "--spec", spec_p])
        self.assertIn("regenerate", err)

    def test_observe_out_to_nonexistent_dir_exit_2(self):
        text = har_text(repeat({"id": 1}, 1))
        tmp = tempfile.mkdtemp()
        hp = self._write(tmp, "t.har", text)
        code, _o, err = run_cli(["observe", "--har", hp, "--out",
                                 os.path.join(tmp, "nope", "b.json")])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)


class TestD2Base64AndEntryErrors(unittest.TestCase):
    def test_invalid_base64_json_body_is_incomplete(self):
        text = har_text([("GET", "https://x/u/1", 200,
                          ("!!!not-base64!!!", "application/json", "base64"))])
        with self.assertRaises(IncompleteInputError):
            har_mod.parse_har_text(text)

    def test_invalid_utf8_base64_json_body_is_incomplete(self):
        import base64 as b64
        payload = b64.b64encode(b"\xff\xfe{}").decode()
        text = har_text([("GET", "https://x/u/1", 200,
                          (payload, "application/json", "base64"))])
        with self.assertRaises(IncompleteInputError):
            har_mod.parse_har_text(text)

    def test_invalid_base64_nonjson_is_skipped_not_error(self):
        text = har_text([("GET", "https://x/u/1", 200,
                          ("!!!", "image/png", "base64"))])
        entries, _ = har_mod.parse_har_text(text)
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].has_body)

    def test_valid_base64_json_body_parsed(self):
        import base64 as b64
        payload = b64.b64encode(json.dumps({"id": 7}).encode()).decode()
        text = har_text([("GET", "https://x/u/1", 200,
                          (payload, "application/json", "base64"))])
        entries, _ = har_mod.parse_har_text(text)
        self.assertTrue(entries[0].has_body)
        self.assertEqual(entries[0].body, {"id": 7})


class TestD3ActionSimulation(unittest.TestCase):
    def test_action_yml_uses_action_path_pythonpath(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "action.yml")) as f:
            yml = f.read()
        self.assertIn("github.action_path", yml)
        self.assertIn("PYTHONPATH", yml)

    def test_consumer_workspace_simulation(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        tmp = tempfile.mkdtemp()
        action_dir = os.path.join(tmp, "action")
        consumer = os.path.join(tmp, "consumer")
        os.makedirs(consumer)
        import shutil
        shutil.copytree(os.path.join(root, "lcdiff"), os.path.join(action_dir, "lcdiff"))
        # Consumer workspace holds ONLY baseline + spec, addressed relatively.
        entries, _ = har_mod.load_har(os.path.join(root, "tests", "fixtures", "clean.har"))
        with open(os.path.join(consumer, "baseline.json"), "w") as f:
            json.dump(inference.build_baseline(entries), f)
        shutil.copy(os.path.join(root, "tests", "fixtures", "clean.openapi.json"),
                    os.path.join(consumer, "openapi.json"))
        env = dict(os.environ, PYTHONPATH=action_dir)
        r = subprocess.run([sys.executable, "-m", "lcdiff", "diff",
                            "--baseline", "baseline.json", "--spec", "openapi.json"],
                           cwd=consumer, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No drift", r.stdout)


class TestD4ReportInjection(unittest.TestCase):
    def test_newline_field_name_cannot_forge_lines(self):
        evil = "evil\n[BREAKING] FORGED"
        b = baseline_from(repeat({evil: 1, "id": 1}, 6))
        docs = spec_docs({"/u/{id}": op(obj_schema(
            {"id": {"type": "integer"}}, required=("id",)))})
        report = diff_mod.diff_report(b, docs)
        text = diff_mod.format_text(report["findings"])
        for line in text.splitlines():
            self.assertFalse(line.startswith("[BREAKING] FORGED"), line)
        self.assertIn("\\n", text)


class TestD5StrictJson(unittest.TestCase):
    def test_nan_body_rejected(self):
        text = har_text([("GET", "https://x/u/1", 200,
                          ('{"x": NaN}', "application/json", None))])
        with self.assertRaises(IncompleteInputError):
            har_mod.parse_har_text(text)

    def test_duplicate_keys_in_spec_exit_2(self):
        tmp = tempfile.mkdtemp()
        bp = os.path.join(tmp, "b.json")
        code, _o, _e = run_cli(["observe", "--har", os.path.join(
            os.path.dirname(__file__), "fixtures", "clean.har"), "--out", bp])
        self.assertEqual(code, 0)
        sp = os.path.join(tmp, "spec.json")
        with open(sp, "w") as f:
            f.write('{"openapi": "3.0.3", "openapi": "3.0.3", "paths": {}}')
        code, _o, err = run_cli(["diff", "--baseline", bp, "--spec", sp])
        self.assertEqual(code, 2)
        self.assertIn("duplicate", err)

    def test_duplicate_keys_in_body_counted_as_warning(self):
        text = har_text([("GET", "https://x/u/1", 200,
                          ('{"a": 1, "a": 2}', "application/json", None))])
        entries, _ = har_mod.parse_har_text(text)
        b = inference.build_baseline(entries)
        self.assertIn("warnings", b)
        self.assertIn("duplicate", b["warnings"][0])
        # last value wins
        self.assertEqual(entries[0].body, {"a": 2})


class TestD6SpecHandling(unittest.TestCase):
    def test_allof_deep_merge_properties(self):
        components = {"schemas": {
            "Base": obj_schema({"cfg": obj_schema(
                {"x": {"type": "integer"}}, required=("x",))}),
            "Ext": {"allOf": [
                {"$ref": "#/components/schemas/Base"},
                obj_schema({"cfg": obj_schema(
                    {"y": {"type": "string"}}, required=("y",))})]}}}
        docs = spec_docs({"/u/{id}": op({"$ref": "#/components/schemas/Ext"})},
                         components=components)
        fields = docs[("GET", "/u/{id}")]["200"]
        self.assertIn("cfg.x", fields)  # wholesale replace would have lost it
        self.assertIn("cfg.y", fields)
        self.assertTrue(fields["cfg.x"]["required"])
        self.assertTrue(fields["cfg.y"]["required"])

    def test_trace_method_recognised(self):
        docs = spec_docs({"/u/{id}": op(obj_schema({"id": {"type": "integer"}}),
                                         method="trace")})
        self.assertIn(("TRACE", "/u/{id}"), docs)

    def test_spec_version_and_paths_validation(self):
        tmp = tempfile.mkdtemp()

        def load(text):
            p = os.path.join(tmp, "s.json")
            with open(p, "w") as f:
                f.write(text)
            return openapi.load_spec(p)

        with self.assertRaises(IncompleteInputError):
            load('{"openapi": "4.0.0", "paths": {"/x": {}}}')
        with self.assertRaises(IncompleteInputError):
            load('{"openapi": "3.0.3", "paths": {}}')
        ok = load('{"openapi": "3.1.0", "paths": {"/x": {}}}')
        self.assertEqual(ok["openapi"], "3.1.0")


class TestD7FormatHints(unittest.TestCase):
    def test_bare_date_is_date_not_datetime(self):
        self.assertEqual(inference.format_hint("2026-01-15"), "date")

    def test_datetime_hint_and_range_checks(self):
        self.assertEqual(inference.format_hint("2026-01-15T10:30:00Z"), "date-time")
        self.assertIsNone(inference.format_hint("2026-13-01"))
        self.assertIsNone(inference.format_hint("2026-01-01T25:00:00"))
        self.assertIsNone(inference.format_hint("2026-01-01T10:61:00"))
        self.assertIsNone(inference.format_hint("2026-01-15 trailing"))

    def test_baseline_records_date_hint(self):
        b = baseline_from(repeat({"d": "2026-01-15"}, 2))
        f = fields_of(b, "GET /u/{id}")["d"]
        self.assertEqual(f["format_hints"], ["date"])


# ------------------------------------------------------------------ E. Coverage

class TestE2ExtraCoverage(unittest.TestCase):
    def test_missing_ref_rejected(self):
        with self.assertRaises(IncompleteInputError):
            spec_docs({"/u/{id}": op({"$ref": "#/components/schemas/Nope"})})

    def test_remote_ref_rejected(self):
        with self.assertRaises(IncompleteInputError):
            spec_docs({"/u/{id}": op({"$ref": "https://example.com/x.json#/Thing"})})

    def test_nonstring_ref_rejected(self):
        with self.assertRaises(IncompleteInputError):
            spec_docs({"/u/{id}": op({"$ref": 42})})

    def test_samples_dir_and_manifest(self):
        tmp = tempfile.mkdtemp()
        sdir = os.path.join(tmp, "samples")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "one.json"), "w") as f:
            json.dump({"id": 1, "name": "Ada"}, f)
        manifest = {"samples": [{"method": "GET", "path": "/users/1",
                                 "status": 200, "file": "one.json"}]}
        mpath = os.path.join(tmp, "manifest.json")
        with open(mpath, "w") as f:
            json.dump(manifest, f)
        out = os.path.join(tmp, "b.json")
        code, _o, err = run_cli(["observe", "--samples-dir", sdir,
                                 "--manifest", mpath, "--out", out])
        self.assertEqual(code, 0, err)
        with open(out) as f:
            b = json.load(f)
        self.assertIn("GET /users/{id}", b["endpoints"])
        self.assertIn("name", fields_of(b, "GET /users/{id}"))

    def test_openapi_31_type_list_nullable(self):
        tmp = tempfile.mkdtemp()
        spec = {"openapi": "3.1.0", "info": {"title": "t", "version": "1"},
                "paths": {"/u/{id}": op(obj_schema(
                    {"nick": {"type": ["string", "null"]}}))}}
        sp = os.path.join(tmp, "spec.json")
        with open(sp, "w") as f:
            json.dump(spec, f)
        loaded = openapi.load_spec(sp)
        docs = openapi.spec_endpoints(loaded)
        specs = repeat({"nick": "x"}, 3) + repeat({"nick": None}, 3)
        findings = diff_mod.diff_baseline_vs_spec(baseline_from(specs), docs)
        self.assertEqual(findings, [])

    def test_incomplete_message_content_templated_via_cli(self):
        token = "abc123def456ghi789jkl012"
        tmp = tempfile.mkdtemp()
        hp = os.path.join(tmp, "t.har")
        with open(hp, "w") as f:
            f.write(har_text([("GET", f"https://x/reset/{token}", 200,
                               ('{"a": ', "application/json", None))]))
        code, _o, err = run_cli(["observe", "--har", hp])
        self.assertEqual(code, 2)
        self.assertIn("INCOMPLETE", err)
        self.assertNotIn(token, err)
        self.assertIn("/reset/{id}", err)


if __name__ == "__main__":
    unittest.main()
