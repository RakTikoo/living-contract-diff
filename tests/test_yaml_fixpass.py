"""YAML fix pass (2026-11-11): one regression test per finding from the
three-reviewer attack on lcdiff/yamlmini.py (items 1-15 of the fix
brief). PyYAML, when installed, is used ONLY as a test oracle.
"""
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lcdiff import cli, openapi, yamlmini  # noqa: E402
from lcdiff.har import IncompleteInputError  # noqa: E402
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


def assertYamlError(case, text, *needles):
    with case.assertRaises(YamlMiniError) as cm:
        yamlmini.loads(text)
    for n in needles:
        case.assertIn(n, str(cm.exception))
    return str(cm.exception)


class Item1EmptySequenceItems(unittest.TestCase):
    def test_bare_dashes_are_none_siblings(self):
        self.assertEqual(yamlmini.loads("-\n-\n-\n"), [None, None, None])

    def test_empty_item_between_values(self):
        self.assertEqual(yamlmini.loads("- a\n-\n- b"), ["a", None, "b"])

    def test_mapping_value_indentless_sequence_still_works(self):
        # The same-indent-dash rule is valid for MAPPING values only.
        self.assertEqual(yamlmini.loads("key:\n- a\n"), {"key": ["a"]})

    def test_items_empty_then_value(self):
        self.assertEqual(yamlmini.loads("items:\n-\n- 1\n"),
                         {"items": [None, 1]})

    def test_250_bare_dashes_no_spurious_depth_error(self):
        self.assertEqual(yamlmini.loads("-\n" * 250), [None] * 250)

    def test_empty_item_with_more_indented_child(self):
        self.assertEqual(yamlmini.loads("-\n  - a\n  - b\n-\n  x: 1\n"),
                         [["a", "b"], {"x": 1}])


class Item2ResponseCodeSmuggling(unittest.TestCase):
    SMUGGLED = (
        "openapi: 3.0.3\ninfo: {title: T, version: '1'}\npaths:\n"
        "  /users:\n    get:\n      responses:\n"
        "        200:\n          description: int keyed\n"
        "          content:\n            application/json:\n"
        "              schema: {type: object, properties: "
        "{id: {type: integer}}, required: [id]}\n"
        "        '200':\n          description: str keyed\n"
        "          content:\n            application/json:\n"
        "              schema: {type: object}\n")
    HONEST = (
        "openapi: 3.0.3\ninfo: {title: T, version: '1'}\npaths:\n"
        "  /users:\n    get:\n      responses:\n"
        "        200:\n          description: int keyed\n"
        "          content:\n            application/json:\n"
        "              schema: {type: object, properties: "
        "{id: {type: integer}}, required: [id]}\n")

    def _tmp_spec(self, text, suffix=".yaml"):
        f = tempfile.NamedTemporaryFile(
            "w", suffix=suffix, delete=False)
        f.write(text)
        f.close()
        return f.name

    def test_spec_endpoints_raises_on_int_str_collision(self):
        spec = openapi.load_spec(self._tmp_spec(self.SMUGGLED))
        with self.assertRaises(IncompleteInputError) as cm:
            openapi.spec_endpoints(spec)
        self.assertIn("duplicate response code", str(cm.exception))
        self.assertIn("200", str(cm.exception))

    def test_smuggled_spec_cli_exit_2(self):
        tmp = tempfile.mkdtemp()
        bpath = os.path.join(tmp, "b.json")
        code, _, err = run_cli(["observe", "--har",
                                os.path.join(FX, "clean.har"),
                                "--out", bpath])
        self.assertEqual(code, 0, err)
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec",
                                self._tmp_spec(self.SMUGGLED)])
        self.assertEqual(code, 2)
        self.assertIn("duplicate response code", err)
        self.assertNotIn("Traceback", err)

    def test_honest_single_200_diffs_normally(self):
        tmp = tempfile.mkdtemp()
        bpath = os.path.join(tmp, "b.json")
        code, _, err = run_cli(["observe", "--har",
                                os.path.join(FX, "clean.har"),
                                "--out", bpath])
        self.assertEqual(code, 0, err)
        code, _, err = run_cli(["diff", "--baseline", bpath, "--spec",
                                self._tmp_spec(self.HONEST)])
        self.assertIn(code, (0, 1))  # a normal diff, never exit 2
        docs = openapi.spec_endpoints(
            openapi.load_spec(self._tmp_spec(self.HONEST)))
        self.assertIn("200", docs[("GET", "/users")])

    def test_method_case_collision_raises(self):
        spec = yamlmini.loads(
            "openapi: 3.0.3\npaths:\n  /x:\n"
            "    get: {responses: {'200': {description: ok}}}\n"
            "    GET: {responses: {'200': {description: ok}}}\n")
        with self.assertRaises(IncompleteInputError):
            openapi.spec_endpoints(spec)

    def test_wildcard_case_collision_raises(self):
        spec = yamlmini.loads(
            "openapi: 3.0.3\npaths:\n  /x:\n    get:\n      responses:\n"
            "        2XX: {description: a}\n"
            "        '2xx': {description: b}\n")
        with self.assertRaises(IncompleteInputError):
            openapi.spec_endpoints(spec)


class Item3NumericKeyDuplicates(unittest.TestCase):
    def test_int_and_bool_keys_duplicate(self):
        assertYamlError(self, "1: a\ntrue: b\n", "duplicate key")

    def test_int_and_float_keys_duplicate(self):
        assertYamlError(self, "1: a\n1.0: b\n", "duplicate key")

    def test_zero_and_false_keys_duplicate(self):
        assertYamlError(self, "0: a\nfalse: b\n", "duplicate key")

    def test_negative_zero_flow(self):
        assertYamlError(self, "{-0.0: a, 0.0: b}\n", "duplicate key")

    def test_int_vs_string_key_still_distinct(self):
        self.assertEqual(yamlmini.loads("1: a\n'1': b\n"),
                         {1: "a", "1": "b"})
        self.assertEqual(yamlmini.loads("true: a\n'true': b\n"),
                         {True: "a", "true": "b"})

    def test_prior_duplicate_rejections_kept(self):
        assertYamlError(self, "0x10: a\n16: b\n", "duplicate key")
        assertYamlError(self, "1_000: a\n1000: b\n", "duplicate key")
        assertYamlError(self, "null: a\n~: b\n", "duplicate key")
        assertYamlError(self, "true: a\nTrue: b\n", "duplicate key")


class Item4FoldedBlockScalars(unittest.TestCase):
    CASES = {
        "blank_before_MI": "a: >\n  normal\n\n    code\n  back\n",
        "blank_after_MI": "a: >\n  normal\n    code\n\n  back\n",
        "multi_blanks_normal": "a: >\n  x\n\n\n  y\n",
        "multi_blanks_MI": "a: >\n  x\n\n\n    y\n",
        "MI_end": "a: >\n  normal\n    code\n",
        "MI_blank_MI_explicit": "a: >2\n    x\n\n    y\n",
        "leading_blank1": "a: >\n\n  x\n",
        "leading_blank2": "a: >\n\n\n  x\n",
        "normal_blank_normal": "a: >\n  x\n\n  y\n",
        "no_blank_MI": "a: >\n  x\n    y\n  z\n",
        "strip_variant": "a: >-\n  x\n\n    y\n  z\n",
        "keep_variant": "a: >+\n  x\n\n    y\n\n\n",
        "MI_start_explicit": "a: >2\n    code\n   normal\n",
    }

    @unittest.skipUnless(_pyyaml, "PyYAML oracle not installed")
    def test_folded_matrix_matches_oracle(self):
        for name, text in self.CASES.items():
            with self.subTest(case=name):
                self.assertEqual(yamlmini.loads(text),
                                 _pyyaml.safe_load(text))

    def test_task_reproduction_exact(self):
        self.assertEqual(
            yamlmini.loads(
                "a: >\n  normal line\n\n    code line\n  back normal\n"),
            {"a": "normal line\n\n  code line\nback normal\n"})

    def test_folded_spec_matches_json_twin_end_to_end(self):
        ytext = (
            "openapi: 3.0.3\ninfo: {title: T, version: '1'}\npaths:\n"
            "  /s:\n    get:\n      responses:\n        '200':\n"
            "          description: >\n"
            "            status values\n\n"
            "              active means go\n"
            "            end of list\n"
            "          content:\n            application/json:\n"
            "              schema: {type: object, properties: "
            "{status: {type: string, enum: [active, suspended]}}}\n")
        spec = yamlmini.loads(ytext)
        desc = spec["paths"]["/s"]["get"]["responses"]["200"][
            "description"]
        if _pyyaml is not None:
            self.assertEqual(desc, _pyyaml.safe_load(ytext)["paths"]
                             ["/s"]["get"]["responses"]["200"]
                             ["description"])
        self.assertEqual(desc,
                         "status values\n\n  active means go\n"
                         "end of list\n")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write(ytext)
            path = f.name
        from lcdiff import openapi as _oa
        endpoints = _oa.spec_endpoints(_oa.load_spec(path))
        self.assertEqual(
            endpoints[("GET", "/s")]["200"]["status"]["enum"],
            ["active", "suspended"])


class Item5IncrementalScanningTiming(unittest.TestCase):
    # The pre-fix implementation rescanned the accumulated text on
    # every appended line (O(n^2)): a 4000-entry multi-line flow map
    # took ~18-29 s. These cases use ~2000 entries and a generous 5 s
    # bound; the fixed scanner finishes them in well under 0.1 s.
    def test_multiline_flow_map_timing(self):
        entries = ",\n".join(f"k{i}: {i}" for i in range(2000))
        start = time.time()
        d = yamlmini.loads("a: {" + entries + "}\n")
        elapsed = time.time() - start
        self.assertEqual(len(d["a"]), 2000)
        self.assertLess(elapsed, 5.0)

    def test_unterminated_quote_timing(self):
        text = "a: 'start\n" + "\n".join(
            "line%d" % i for i in range(2000)) + "\n"
        start = time.time()
        with self.assertRaises(YamlMiniError):
            yamlmini.loads(text)
        self.assertLess(time.time() - start, 5.0)


class Item6NodeCounting(unittest.TestCase):
    def test_small_document_exact_count(self):
        # root map 1 + (key+null) 2 + (key+scalar) 2
        # + (key + seq + 2 items) 4 = 9 nodes.
        p = yamlmini._Parser("a:\nb: 1\nc: [1, 2]\n")
        p.parse_document()
        self.assertEqual(p.nodes, 9)

    def test_split_key_probe_burns_no_nodes(self):
        p = yamlmini._Parser("{a: 1}: v\n")
        self.assertIsNotNone(p._split_key("{a: 1}: v", 1))
        self.assertEqual(p.nodes, 0)

    def test_empty_valued_keys_hit_node_limit(self):
        old = yamlmini.MAX_NODES
        yamlmini.MAX_NODES = 200
        try:
            # 150 empty-valued keys = 1 + 150*(key+null) = 301 nodes.
            text = "".join(f"k{i}:\n" for i in range(150))
            with self.assertRaises(YamlMiniError) as cm:
                yamlmini.loads(text)
            self.assertIn("nodes", str(cm.exception))
        finally:
            yamlmini.MAX_NODES = old

    def test_block_scalar_and_flow_implicit_null_counted(self):
        p = yamlmini._Parser("a: |\n  x\nb: {c}\n")
        p.parse_document()
        # root map 1 + key a 1 + block scalar 1
        # + key b 1 + flow map 1 + key c 1 + implicit null 1 = 7.
        self.assertEqual(p.nodes, 7)


class Item7ErrorDiscipline(unittest.TestCase):
    def test_huge_decimal_int_named_error(self):
        assertYamlError(self, "a: " + "9" * 5000 + "\n",
                        "integer literal too long")

    def test_huge_hex_int_key_named_error(self):
        assertYamlError(self, "0x" + "f" * 5000 + ": a\n",
                        "integer literal too long")

    def test_huge_octal_int_named_error(self):
        assertYamlError(self, "a: 0o" + "7" * 5000 + "\n",
                        "integer literal too long")

    def test_escape_out_of_range_named_error(self):
        assertYamlError(self, 'a: "\\U00110000"\n', "out of range")

    def test_lone_surrogate_escapes_named_error(self):
        assertYamlError(self, 'a: "\\uD800"\n', "lone surrogate")
        assertYamlError(self, 'a: "\\U0000DFFF"\n', "lone surrogate")

    def test_valid_escapes_still_work(self):
        self.assertEqual(yamlmini.loads('a: "\\U0001F600"\n')["a"],
                         "\U0001F600")

    def test_huge_int_via_cli_exit_2_clean(self):
        tmp = tempfile.mkdtemp()
        bpath = os.path.join(tmp, "b.json")
        code, _, err = run_cli(["observe", "--har",
                                os.path.join(FX, "clean.har"),
                                "--out", bpath])
        self.assertEqual(code, 0, err)
        p = os.path.join(tmp, "s.yaml")
        with open(p, "w") as f:
            f.write("openapi: 3.0.3\ninfo: {title: T, version: '1'}\n"
                    "paths: {}\nx: " + "9" * 5000 + "\n")
        code, _, err = run_cli(["diff", "--baseline", bpath,
                                "--spec", p])
        self.assertEqual(code, 2)
        self.assertIn("integer literal too long", err)
        self.assertNotIn("Traceback", err)


class Item8UnicodeLineBreaks(unittest.TestCase):
    def test_u2028_splits_lines(self):
        self.assertEqual(yamlmini.loads("a: 1\u2028b: 2\n"),
                         {"a": 1, "b": 2})

    def test_u2029_and_nel_split_lines(self):
        self.assertEqual(yamlmini.loads("a: 1\u2029b: 2\n"),
                         {"a": 1, "b": 2})
        self.assertEqual(yamlmini.loads("a: 1\x85b: 2\n"),
                         {"a": 1, "b": 2})


class Item9ExplicitKeysInFlow(unittest.TestCase):
    def test_flow_explicit_key_rejected(self):
        assertYamlError(self, "a: {? k: 1}\n", "explicit mapping keys")

    def test_flow_sequence_explicit_item_rejected(self):
        assertYamlError(self, "a: [? k]\n", "explicit mapping keys")

    def test_block_explicit_key_still_rejected(self):
        assertYamlError(self, "? k\n: 1\n", "explicit mapping keys")


class Item10PlainScalarColonRule(unittest.TestCase):
    def test_value_with_colon_space_rejected(self):
        assertYamlError(self, "a: b: c\n",
                        "mapping values are not allowed here")

    def test_nested_value_with_colon_space_rejected(self):
        assertYamlError(self, "a:\n  - b: c: d\n",
                        "mapping values are not allowed here")

    def test_trailing_colon_rejected(self):
        assertYamlError(self, "a: b:\n",
                        "mapping values are not allowed here")

    def test_continuation_line_colon_space_rejected(self):
        assertYamlError(self, "a: one\n  two: three\n",
                        "mapping values are not allowed here")

    def test_colons_without_space_still_fine(self):
        d = yamlmini.loads(
            "a: http://x:8080/p\nb: 12:34:56\nc: time 10:30 now\n")
        self.assertEqual(d, {"a": "http://x:8080/p", "b": "12:34:56",
                             "c": "time 10:30 now"})

    def test_quoted_colon_space_unaffected(self):
        self.assertEqual(yamlmini.loads("a: 'b: c'\n"), {"a": "b: c"})
        self.assertEqual(yamlmini.loads('a: "b: c"\n'), {"a": "b: c"})


class Item11MiscBundle(unittest.TestCase):
    def test_directive_named_error(self):
        assertYamlError(self, "%YAML 1.2\n---\na: 1\n", "directive")
        assertYamlError(self, "%TAG ! tag:example.com,2026:\n---\n"
                        "a: 1\n", "directive")

    def test_end_marker_trailing_junk_named_error(self):
        assertYamlError(self, "a: 1\n... foo\n", "document-end")

    def test_end_marker_with_comment_ok(self):
        self.assertEqual(yamlmini.loads("a: 1\n... # done\n"), {"a": 1})

    def test_keep_chomped_all_blank_scalars(self):
        self.assertEqual(yamlmini.loads("a: |+\n\n\n"), {"a": "\n\n"})
        self.assertEqual(yamlmini.loads("a: >+\n\n\n"), {"a": "\n\n"})
        self.assertEqual(yamlmini.loads("a: |\n\n\n"), {"a": ""})
        self.assertEqual(yamlmini.loads("a: |-\n\n\n"), {"a": ""})
        self.assertEqual(yamlmini.loads("a: |+\n\n\nb: 1\n"),
                         {"a": "\n\n", "b": 1})

    def test_reserved_indicators_named_error(self):
        assertYamlError(self, "a: @foo\n", "reserved indicator")
        assertYamlError(self, "a: `foo`\n", "reserved indicator")

    def test_boolean_spellings_restricted_to_core(self):
        self.assertEqual(yamlmini.loads("a: tRuE\n"), {"a": "tRuE"})
        self.assertEqual(yamlmini.loads("a: TrUe\n"), {"a": "TrUe"})
        self.assertEqual(yamlmini.loads("a: TRUE\n"), {"a": True})
        self.assertEqual(yamlmini.loads("a: False\n"), {"a": False})

    def test_tab_as_separation_rejected(self):
        assertYamlError(self, "a:\t1\n", "tab")
        assertYamlError(self, "-\t1\n", "tab")
        assertYamlError(self, "- a\n-\t1\n", "tab")


class Item12TabsInBlockScalarContent(unittest.TestCase):
    def test_tab_indented_code_in_literal_scalar(self):
        text = "a: |\n  code\n  \tmore\n"
        self.assertEqual(yamlmini.loads(text), {"a": "code\n\tmore\n"})
        if _pyyaml is not None:
            self.assertEqual(yamlmini.loads(text),
                             _pyyaml.safe_load(text))

    def test_tab_content_in_folded_scalar(self):
        text = "desc: >\n  first\n  \tsecond\n"
        if _pyyaml is not None:
            self.assertEqual(yamlmini.loads(text),
                             _pyyaml.safe_load(text))

    def test_structural_tab_indentation_still_rejected(self):
        assertYamlError(self, "a:\n\tb: 1\n", "tab", "line 2")
        assertYamlError(self, "a: 1\n\tb: 2\n", "tab")


class Item13GluedCommentsAfterQuoted(unittest.TestCase):
    def test_mapping_value_glued_comment(self):
        self.assertEqual(yamlmini.loads("version: '1'# api version\n"),
                         {"version": "1"})

    def test_sequence_item_glued_comment(self):
        self.assertEqual(yamlmini.loads("- '1'# c\n- 2\n"), ["1", 2])

    def test_double_quoted_glued_comment(self):
        self.assertEqual(yamlmini.loads('a: "x"# c\n'), {"a": "x"})

    def test_flow_glued_comment_still_rejected(self):
        assertYamlError(self, "a: {k: 'v'# c}\n", "flow")


class Item14DepthBoundary(unittest.TestCase):
    @staticmethod
    def _flow_seq(n):
        return "[" * n + "1" + "]" * n + "\n"

    @staticmethod
    def _flow_map(n):
        s = "1"
        for _ in range(n):
            s = "{a: " + s + "}"
        return s + "\n"

    @staticmethod
    def _block_map(n):
        lines = ["  " * i + "k:" for i in range(n - 1)]
        lines.append("  " * (n - 1) + "k: 1")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _block_seq(n):
        lines = ["  " * i + "-" for i in range(n - 1)]
        lines.append("  " * (n - 1) + "- 1")
        return "\n".join(lines) + "\n"

    def test_both_syntaxes_allow_200_error_201(self):
        builders = (self._flow_seq, self._flow_map,
                    self._block_map, self._block_seq)
        for build in builders:
            with self.subTest(shape=build.__name__):
                self.assertIsNotNone(yamlmini.loads(build(200)))
                assertYamlError(self, build(201), "depth")


class Item15DecodeHint(unittest.TestCase):
    def test_utf16_yaml_load_spec_hints_utf8(self):
        f = tempfile.NamedTemporaryFile("wb", suffix=".yaml",
                                        delete=False)
        f.write("openapi: 3.0.3\n".encode("utf-16"))
        f.close()
        with self.assertRaises(IncompleteInputError) as cm:
            openapi.load_spec(f.name)
        self.assertIn("UTF-8", str(cm.exception))

    def test_utf16_yaml_cli_exit_2(self):
        tmp = tempfile.mkdtemp()
        bpath = os.path.join(tmp, "b.json")
        code, _, err = run_cli(["observe", "--har",
                                os.path.join(FX, "clean.har"),
                                "--out", bpath])
        self.assertEqual(code, 0, err)
        p = os.path.join(tmp, "u.yaml")
        with open(p, "wb") as f:
            f.write("openapi: 3.0.3\n".encode("utf-16"))
        code, _, err = run_cli(["diff", "--baseline", bpath,
                                "--spec", p])
        self.assertEqual(code, 2)
        self.assertIn("UTF-8", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
