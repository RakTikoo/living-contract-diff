"""YAML spec support (v0.2): the yamlmini subset parser, the JSON/YAML
equivalence corpus, scalar-typing pins, named rejections, safety
limits and CLI discipline.

PyYAML, when installed, is used ONLY here as a test oracle — nothing
under lcdiff/ imports it (pinned by test_no_third_party_yaml_in_package).
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lcdiff import cli, openapi, yamlmini  # noqa: E402
from lcdiff.yamlmini import YamlMiniError  # noqa: E402

FX = os.path.join(os.path.dirname(__file__), "fixtures")

try:
    import yaml as _pyyaml
except ImportError:  # pragma: no cover
    _pyyaml = None


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def canon(v):
    """Typed canonical form: distinguishes int/float/bool/str keys and
    values exactly (1 != 1.0 != True != '1')."""
    if isinstance(v, bool):
        return ("bool", v)
    if isinstance(v, int):
        return ("int", v)
    if isinstance(v, float):
        return ("float", repr(v))
    if isinstance(v, str):
        return ("str", v)
    if v is None:
        return ("null", None)
    if isinstance(v, list):
        return ("list", tuple(canon(x) for x in v))
    if isinstance(v, dict):
        return ("dict", tuple(sorted((canon(k), canon(x))
                                      for k, x in v.items())))
    raise TypeError(repr(v))


def fixture_names():
    return sorted(os.path.basename(p)[:-len(".openapi.json")]
                  for p in __import__("glob").glob(
                      os.path.join(FX, "*.openapi.json")))


def har_for(name):
    p = os.path.join(FX, name + ".har")
    if os.path.exists(p):
        return p
    return os.path.join(FX, name + "_ok.har")  # intfloat_ok.har


class EquivalenceCorpus(unittest.TestCase):
    """Every tests/fixtures/*.openapi.json has a YAML twin; for each
    pair, the parsed structures are typed-identical and the full CLI
    pipeline (same HAR -> baseline -> diff, JSON report) produces
    identical reports and exit codes."""


def _make_equivalence_test(name):
    def test(self):
        jpath = os.path.join(FX, name + ".openapi.json")
        ypath = os.path.join(FX, name + ".openapi.yaml")
        self.assertTrue(os.path.exists(ypath), f"missing YAML twin {ypath}")
        self.assertEqual(canon(openapi.load_spec(jpath)),
                         canon(openapi.load_spec(ypath)))
        tmp = tempfile.mkdtemp()
        bpath = os.path.join(tmp, "b.json")
        code, _, err = run_cli(["observe", "--har", har_for(name),
                                "--out", bpath])
        self.assertEqual(code, 0, err)
        cj, outj, errj = run_cli(["diff", "--baseline", bpath, "--spec",
                                  jpath, "--format", "json"])
        cy, outy, erry = run_cli(["diff", "--baseline", bpath, "--spec",
                                  ypath, "--format", "json"])
        self.assertEqual(cj, cy, f"{name}: exit {cj} (json) vs {cy} (yaml)")
        self.assertEqual(json.loads(outj), json.loads(outy),
                         f"{name}: JSON and YAML reports differ")
    test.__name__ = f"test_equivalence_{name}"
    return test


for _name in fixture_names():
    setattr(EquivalenceCorpus, f"test_equivalence_{_name}",
            _make_equivalence_test(_name))


class HandwrittenStyle(unittest.TestCase):
    def test_generator_style_fixture_zero_findings(self):
        from lcdiff import har as har_mod, inference
        from lcdiff import diff as diff_mod
        spec = openapi.load_spec(
            os.path.join(FX, "handwritten_style.openapi.yaml"))
        entries, _ = har_mod.load_har(os.path.join(FX, "clean.har"))
        baseline = inference.build_baseline(entries)
        findings = diff_mod.diff_baseline_vs_spec(
            baseline, openapi.spec_endpoints(spec))
        self.assertEqual(findings, [])
        # The documented contract is identical to the JSON fixture's.
        jspec = openapi.load_spec(os.path.join(FX, "clean.openapi.json"))
        self.assertEqual(openapi.spec_endpoints(spec),
                         openapi.spec_endpoints(jspec))

    @unittest.skipUnless(_pyyaml, "PyYAML oracle not installed")
    def test_oracle_agrees_on_all_twins(self):
        for name in fixture_names():
            with open(os.path.join(FX, name + ".openapi.yaml")) as f:
                oracle = _pyyaml.safe_load(f)
            with open(os.path.join(FX, name + ".openapi.json")) as f:
                want = json.load(f)
            self.assertEqual(canon(oracle), canon(want), name)

    @unittest.skipUnless(_pyyaml, "PyYAML oracle not installed")
    def test_oracle_agrees_on_curated_document(self):
        text = (
            "name: widget\nprice: 9.99\ncount: 3\nok: true\n"
            "nested: {a: [1, 2.5, null], b: 'x: y'}\n"
            "items:\n  - id: 1\n    tags: [a, b]\n  - id: 2\n"
            "desc: |\n  literal text\n")
        self.assertEqual(canon(yamlmini.loads(text)),
                         canon(_pyyaml.safe_load(text)))


class ScalarTyping(unittest.TestCase):
    def test_openapi_version_number_matches_json(self):
        v = yamlmini.loads("openapi: 3.1\n")["openapi"]
        self.assertIsInstance(v, float)
        self.assertEqual(v, json.loads("3.1"))

    def test_version_310_float_like_json(self):
        # Documented: plain 3.10 -> float 3.1, exactly as json.loads
        # reads 3.10. No string preservation is invented.
        v = yamlmini.loads("version: 3.10\n")["version"]
        self.assertIsInstance(v, float)
        self.assertEqual(v, 3.1)

    def test_timestamp_stays_string(self):
        v = yamlmini.loads("d: 2026-01-01\n")["d"]
        self.assertEqual(v, "2026-01-01")
        self.assertIsInstance(v, str)

    def test_leading_zero_decimal_is_string(self):
        # YAML 1.2 core decision (PyYAML 1.1 would give octal 10).
        self.assertEqual(yamlmini.loads("v: 012\n")["v"], "012")
        self.assertEqual(yamlmini.loads("v: 0\n")["v"], 0)
        self.assertEqual(yamlmini.loads("v: 10\n")["v"], 10)

    def test_hex_and_octal_ints_sexagesimal_string(self):
        d = yamlmini.loads("h: 0x1F\no: 0o17\nneg: -0x10\ns: 1:30\n")
        self.assertEqual(d, {"h": 31, "o": 15, "neg": -16, "s": "1:30"})

    def test_float_10_distinct_from_int_1(self):
        d = yamlmini.loads("a: 1.0\nb: 1\nc: 1e3\nd: .5\ne: -2.5e-2\n")
        self.assertIsInstance(d["a"], float)
        self.assertIsInstance(d["b"], int)
        self.assertNotIsInstance(d["b"], bool)
        self.assertIsInstance(d["c"], float)
        self.assertEqual(d["c"], 1000.0)
        self.assertEqual(d["d"], 0.5)
        self.assertEqual(d["e"], -0.025)

    def test_inf_nan_mirror_pyyaml(self):
        import math
        d = yamlmini.loads("a: .inf\nb: -.inf\nc: .INF\nd: .nan\n")
        self.assertEqual(d["a"], float("inf"))
        self.assertEqual(d["b"], float("-inf"))
        self.assertEqual(d["c"], float("inf"))
        self.assertTrue(math.isnan(d["d"]))

    def test_booleans_only_true_false_any_case(self):
        d = yamlmini.loads(
            "a: true\nb: True\nc: TRUE\nd: false\ne: False\nf: FALSE\n")
        self.assertEqual(d, {"a": True, "b": True, "c": True,
                             "d": False, "e": False, "f": False})
        s = yamlmini.loads("a: yes\nb: no\nc: on\nd: off\ne: y\nf: n\n")
        self.assertEqual(s, {"a": "yes", "b": "no", "c": "on",
                             "d": "off", "e": "y", "f": "n"})

    def test_null_spellings(self):
        d = yamlmini.loads("a: null\nb: Null\nc: NULL\nd: ~\ne:\n")
        self.assertEqual(d, {"a": None, "b": None, "c": None,
                             "d": None, "e": None})

    def test_underscored_and_negative_numbers(self):
        d = yamlmini.loads("a: 1_000\nb: -42\nc: +7\n")
        self.assertEqual(d, {"a": 1000, "b": -42, "c": 7})

    def test_int_key_vs_string_key_distinct(self):
        self.assertEqual(yamlmini.loads("1: a\n'1': b\n"), {1: "a", "1": "b"})


class SyntaxFeatures(unittest.TestCase):
    def test_block_scalars_all_flavours(self):
        d = yamlmini.loads(
            "lit: |\n  a\n  b\nstrip: |-\n  a\n  b\nkeep: |+\n  a\n\n\n"
            "fold: >\n  a b\n  c d\n\n  e f\nfoldstrip: >-\n  a\n  b\n")
        self.assertEqual(d["lit"], "a\nb\n")
        self.assertEqual(d["strip"], "a\nb")
        self.assertEqual(d["keep"], "a\n\n\n")
        self.assertEqual(d["fold"], "a b c d\ne f\n")
        self.assertEqual(d["foldstrip"], "a b")

    def test_block_scalar_explicit_indent(self):
        self.assertEqual(yamlmini.loads("d: |2\n   x\n   y\n")["d"], " x\n y\n")

    def test_flow_nested_and_mixed(self):
        d = yamlmini.loads(
            "a: {x: [1, {y: 2}], b: {}}\nlist: [{k: v}, [3], 'four']\n")
        self.assertEqual(d, {"a": {"x": [1, {"y": 2}], "b": {}},
                             "list": [{"k": "v"}, [3], "four"]})

    def test_compact_and_indentless_sequences(self):
        d = yamlmini.loads(
            "req:\n- id\n- name\nitems:\n  - name: a\n    v: 1\n  - name: b\n")
        self.assertEqual(d, {"req": ["id", "name"],
                             "items": [{"name": "a", "v": 1}, {"name": "b"}]})

    def test_quoted_escapes(self):
        d = yamlmini.loads(
            "a: 'it''s'\n"
            'b: "n\\n t\\t q\\" bs\\\\ x\\x41 u\\u00e9 U\\U0001f600"\n')
        self.assertEqual(d["a"], "it's")
        self.assertEqual(d["b"], 'n\n t\t q" bs\\ xA ué U\U0001f600')

    def test_comments_and_hash_in_strings(self):
        d = yamlmini.loads(
            "# lead\na: 1 # trail\nb: a#b\nc: 'x # y'\nd: https://x/#frag\n")
        self.assertEqual(d, {"a": 1, "b": "a#b", "c": "x # y",
                             "d": "https://x/#frag"})

    def test_special_keys(self):
        d = yamlmini.loads(
            "'quoted key': 1\nhttp://example.com/a: 2\n/users/{id}: 3\n"
            "a#b: 4\n")
        self.assertEqual(d, {"quoted key": 1, "http://example.com/a": 2,
                             "/users/{id}": 3, "a#b": 4})

    def test_values_with_colons_and_braces(self):
        d = yamlmini.loads("a: prefix{suffix}\nb: time 10:30 now\n")
        self.assertEqual(d["a"], "prefix{suffix}")
        self.assertEqual(d["b"], "time 10:30 now")

    def test_multiline_plain_and_quoted(self):
        d = yamlmini.loads(
            "a: one\n  two three\nb: 'one\n  two'\n")
        self.assertEqual(d, {"a": "one two three", "b": "one two"})

    def test_document_markers(self):
        self.assertEqual(yamlmini.loads("---\na: 1\n...\n"), {"a": 1})
        self.assertEqual(yamlmini.loads("--- {a: 1}\n"), {"a": 1})

    def test_crlf_and_unicode(self):
        d = yamlmini.loads("a: café 🎉\r\nb: 2\r\n")
        self.assertEqual(d, {"a": "café 🎉", "b": 2})

    def test_deeply_mixed_nesting(self):
        d = yamlmini.loads(
            "paths:\n  /x:\n    get:\n      responses:\n        '200':\n"
            "          content:\n            application/json:\n"
            "              schema: {type: object, properties: {id: {type: integer}}}\n")
        schema = d["paths"]["/x"]["get"]["responses"]["200"]["content"][
            "application/json"]["schema"]
        self.assertEqual(schema["properties"]["id"]["type"], "integer")


class Rejections(unittest.TestCase):
    def assertYamlError(self, text, *needles):
        with self.assertRaises(YamlMiniError) as cm:
            yamlmini.loads(text)
        for n in needles:
            self.assertIn(n, str(cm.exception))

    def test_anchor_rejected_named(self):
        self.assertYamlError("a: &x 1\n", "anchor")

    def test_alias_rejected_named(self):
        self.assertYamlError("a: *x\n", "alias")

    def test_alias_bomb_rejected_fast(self):
        bomb = ("a: &a [lol,lol,lol]\nb: &b [*a,*a,*a]\n"
                "c: &c [*b,*b,*b]\nd: [*c,*c,*c]\n")
        self.assertYamlError(bomb, "anchor")

    def test_merge_key_rejected_named(self):
        self.assertYamlError("a:\n  <<: 1\n", "merge")
        self.assertYamlError("a: {<<: 1}\n", "merge")

    def test_custom_tag_rejected_named(self):
        self.assertYamlError("a: !!str 1\n", "tag")
        self.assertYamlError("a: !foo bar\n", "tag")

    def test_multi_document_rejected(self):
        self.assertYamlError("a: 1\n---\nb: 2\n", "multiple YAML documents")

    def test_tab_indentation_rejected(self):
        self.assertYamlError("a:\n\tb: 1\n", "tab", "line 2")

    def test_inconsistent_indentation_rejected_with_line(self):
        self.assertYamlError("a: one\n  b: two\n", "line 2")

    def test_duplicate_keys_everywhere(self):
        self.assertYamlError("a: 1\na: 2\n", "duplicate key")
        self.assertYamlError("a:\n  x: 1\n  x: 2\n", "duplicate key")
        self.assertYamlError("a: {x: 1, x: 2}\n", "duplicate key")
        self.assertYamlError("true: 1\nTrue: 2\n", "duplicate key")

    def test_unterminated_constructs(self):
        self.assertYamlError("a: {x: 1\n", "flow")
        self.assertYamlError("a: 'open\n", "unterminated")

    def test_unknown_escape_rejected(self):
        self.assertYamlError('a: "bad\\q"\n', "escape")


class Limits(unittest.TestCase):
    def test_depth_limit(self):
        text = "a:\n" + "".join(
            " " * (2 * i) + f"k{i}:\n" for i in range(1, 250))
        with self.assertRaises(YamlMiniError) as cm:
            yamlmini.loads(text)
        self.assertIn("depth", str(cm.exception))

    def test_flow_depth_limit(self):
        text = "a: " + "[" * 250 + "]" * 250 + "\n"
        with self.assertRaises(YamlMiniError) as cm:
            yamlmini.loads(text)
        self.assertIn("depth", str(cm.exception))

    def test_node_limit(self):
        old = yamlmini.MAX_NODES
        yamlmini.MAX_NODES = 50
        try:
            with self.assertRaises(YamlMiniError) as cm:
                yamlmini.loads("a: [" + ", ".join(str(i) for i in range(80))
                               + "]\n")
            self.assertIn("nodes", str(cm.exception))
        finally:
            yamlmini.MAX_NODES = old

    def test_size_limit(self):
        big = "a: " + "x" * (yamlmini.MAX_DOCUMENT_BYTES + 1) + "\n"
        with self.assertRaises(YamlMiniError) as cm:
            yamlmini.loads(big)
        self.assertIn("maximum size", str(cm.exception))

    def test_legit_large_spec_well_under_limits(self):
        # 2,000 paths parse fine (documented comfort margin for 10k).
        parts = ["openapi: 3.0.3", "paths:"]
        for i in range(2000):
            parts.append(f"  /p{i}: {{get: {{responses: {{'200': "
                         f"{{description: ok}}}}}}}}")
        d = yamlmini.loads("\n".join(parts) + "\n")
        self.assertEqual(len(d["paths"]), 2000)


class CliYaml(unittest.TestCase):
    def _baseline(self, tmp, har="clean.har"):
        bpath = os.path.join(tmp, "b.json")
        code, _, err = run_cli(["observe", "--har", os.path.join(FX, har),
                                "--out", bpath])
        self.assertEqual(code, 0, err)
        return bpath

    def _write(self, tmp, name, text):
        p = os.path.join(tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_valid_yaml_end_to_end(self):
        tmp = tempfile.mkdtemp()
        bpath = self._baseline(tmp)
        code, out, err = run_cli(["diff", "--baseline", bpath, "--spec",
                                  os.path.join(FX, "clean.openapi.yaml")])
        self.assertEqual(code, 0, err)
        self.assertIn("No drift", out)

    def test_unsupported_construct_exit_2_clear_message(self):
        tmp = tempfile.mkdtemp()
        bpath = self._baseline(tmp)
        p = self._write(tmp, "s.yaml", "openapi: 3.0.3\na: &x 1\npaths: {}\n")
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", p])
        self.assertEqual(code, 2)
        self.assertIn("anchor", err)
        self.assertNotIn("Traceback", err)

    def test_yaml_duplicate_key_exit_2(self):
        tmp = tempfile.mkdtemp()
        bpath = self._baseline(tmp)
        p = self._write(tmp, "s.yaml", "openapi: 3.0.3\nopenapi: 3.0.3\n")
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", p])
        self.assertEqual(code, 2)
        self.assertIn("duplicate key", err)

    def test_sniffing_no_extension(self):
        tmp = tempfile.mkdtemp()
        bpath = self._baseline(tmp)
        with open(os.path.join(FX, "clean.openapi.yaml")) as f:
            ytext = f.read()
        p = self._write(tmp, "spec_noext", ytext)
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", p])
        self.assertEqual(code, 0, err)

    def test_sniffing_json_content_in_txt(self):
        tmp = tempfile.mkdtemp()
        bpath = self._baseline(tmp)
        with open(os.path.join(FX, "clean.openapi.json")) as f:
            jtext = f.read()
        p = self._write(tmp, "spec.txt", jtext)
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", p])
        self.assertEqual(code, 0, err)

    def test_yaml_file_that_is_json_loads(self):
        tmp = tempfile.mkdtemp()
        bpath = self._baseline(tmp)
        with open(os.path.join(FX, "clean.openapi.json")) as f:
            jtext = f.read()
        p = self._write(tmp, "spec.yaml", jtext)
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", p])
        self.assertEqual(code, 0, err)

    def test_empty_file_exit_2(self):
        tmp = tempfile.mkdtemp()
        bpath = self._baseline(tmp)
        p = self._write(tmp, "empty.yaml", "")
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", p])
        self.assertEqual(code, 2)
        self.assertIn("error", err)

    def test_valid_yaml_not_openapi_exit_2(self):
        tmp = tempfile.mkdtemp()
        bpath = self._baseline(tmp)
        p = self._write(tmp, "s.yaml", "just: some\nplain: yaml\n")
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", p])
        self.assertEqual(code, 2)
        self.assertIn("OpenAPI 3", err)

    def test_json_path_unchanged_errors(self):
        tmp = tempfile.mkdtemp()
        bpath = self._baseline(tmp)
        p = self._write(tmp, "s.json", '{"openapi": "3.0.3", "openapi": "3.0.3"}')
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", p])
        self.assertEqual(code, 2)
        self.assertIn("duplicate key", err)
        p = self._write(tmp, "s2.json", '{"openapi": NaN}')
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec", p])
        self.assertEqual(code, 2)
        self.assertIn("not valid strict JSON", err)

    def test_huge_single_line_scalar(self):
        big = "x" * 1_000_000
        d = yamlmini.loads(f"description: {big}\n")
        self.assertEqual(d["description"], big)


class FuzzRegressions(unittest.TestCase):
    """Shapes found by differential fuzzing against the PyYAML oracle
    during development; pinned so they cannot regress silently."""

    def test_triply_nested_compact_sequence_of_mapping(self):
        d = yamlmini.loads("- - - a: true\n    - 1.0\n    - it's\n")
        self.assertEqual(d, [[[{"a": True}, 1.0, "it's"]]])

    def test_apostrophe_inside_flow_plain_scalar(self):
        self.assertEqual(yamlmini.loads("a: {name: it's, n: 1}\n"),
                         {"a": {"name": "it's", "n": 1}})

    def test_flow_plain_scalar_folds_across_lines(self):
        self.assertEqual(
            yamlmini.loads("a: {d: multi\n      word here}\n"),
            {"a": {"d": "multi word here"}})

    def test_unquoted_status_code_end_to_end(self):
        text = (
            "openapi: 3.0.3\ninfo: {title: T, version: '1'}\npaths:\n"
            "  /users/{id}:\n    get:\n      responses:\n        200:\n"
            "          content:\n            application/json:\n"
            "              schema: {type: object, properties: "
            "{id: {type: integer}}, required: [id]}\n")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write(text)
            path = f.name
        docs = openapi.spec_endpoints(openapi.load_spec(path))
        self.assertIn(("GET", "/users/{id}"), docs)
        self.assertIn("200", docs[("GET", "/users/{id}")])
        self.assertTrue(
            docs[("GET", "/users/{id}")]["200"]["id"]["required"])

    @unittest.skipUnless(_pyyaml, "PyYAML oracle not installed")
    def test_seeded_differential_fuzz(self):
        import random
        rng = random.Random(20261111)
        words = ["alpha", "with space", "a#b", "x: y", "yes", "true",
                 "012", "3.10", "on", "it's", "a,b", "- dash", "café",
                 "multi word value here", "don't"]

        def scalar():
            return rng.choice([rng.choice(words), rng.randint(-99, 99),
                               rng.choice([1.0, 2.5]), rng.choice(
                                   [True, False, None]), ""])

        def val(d=0):
            if d > 3:
                return scalar()
            r = rng.random()
            if r < 0.45:
                return scalar()
            if r < 0.7:
                return [val(d + 1) for _ in range(rng.randint(0, 3))]
            return {rng.choice(words) + str(i): val(d + 1)
                    for i in range(rng.randint(0, 3))}

        for _ in range(60):
            doc = {f"k{i}": val() for i in range(rng.randint(1, 4))}
            for fs in (False, True):
                text = _pyyaml.safe_dump(
                    doc, sort_keys=False, allow_unicode=True,
                    width=rng.choice([10 ** 9, 30]), default_flow_style=fs)
                self.assertEqual(canon(yamlmini.loads(text)),
                                 canon(_pyyaml.safe_load(text)), text)


class PackageHygiene(unittest.TestCase):
    def test_no_third_party_yaml_in_package(self):
        import re
        pkg = os.path.join(os.path.dirname(__file__), "..", "lcdiff")
        for fname in os.listdir(pkg):
            if not fname.endswith(".py"):
                continue
            with open(os.path.join(pkg, fname)) as f:
                src = f.read()
            self.assertIsNone(
                re.search(r"^\s*(import\s+yaml|from\s+yaml|import\s+pyyaml)",
                          src, re.M),
                f"{fname} imports a third-party YAML library")
            self.assertNotIn("import yaml", src.replace("import yamlmini", ""))


if __name__ == "__main__":
    unittest.main()
