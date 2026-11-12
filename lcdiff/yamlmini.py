"""A YAML-subset parser for OpenAPI specs — Python stdlib only.

This is a leaf module: it converts YAML text into the same Python
structures ``json.loads`` would produce (dict / list / str / int /
float / bool / None), so the rest of LCD never knows which syntax a
spec arrived in. It imports nothing from the rest of lcdiff; the
caller (openapi.py) translates YamlMiniError into IncompleteInputError.

SUPPORTED SUBSET (real-world OpenAPI YAML: FastAPI / Swagger /
Springdoc / hand-written):

- block mappings and sequences by indentation, nesting, compact
  sequences of mappings (``- key: value``), indentless sequences under
  a mapping key (``key:`` followed by ``- item`` at the key's indent)
- flow mappings / sequences ``{..}`` / ``[..]``, nested, flow inside
  block and block values around flow, multi-line flow collections
- plain, single-quoted (``''`` escaping) and double-quoted scalars
  (full YAML double-quote escapes: \\n \\t \\" \\\\ \\xNN \\uNNNN
  \\UNNNNNNNN and the rest of the YAML escape table)
- block scalars ``|`` and ``>`` with chomping indicators (``+``/``-``)
  and explicit indent indicators
- comments (``#`` at comment position only), multi-line plain scalars
  (continuation lines fold with a single space)
- quoted keys; keys/values containing colons not followed by a space
  (URLs), braces and ``#`` not at comment position
- a single leading ``---`` document marker; a trailing ``...``
  document-end marker is allowed

SCALAR TYPING (YAML 1.2 core schema, mirroring JSON where they meet):

- ``true``/``True``/``TRUE`` and ``false``/``False``/``FALSE`` -> bool
  (the YAML 1.2 core spellings only; any other casing, e.g. ``tRuE``,
  is a STRING). ``yes``/``no``/``on``/``off`` are STRINGS, never bools
  (YAML 1.2 dropped the 1.1 spellings).
- ``null`` / ``Null`` / ``NULL`` / ``~`` / empty value -> None.
- Integers: decimal (underscores allowed, e.g. ``1_000``), ``0x`` hex
  and ``0o`` octal. A decimal with a leading zero (``012``) is a STRING
  per the YAML 1.2 core schema — PyYAML (YAML 1.1) reads it as octal
  10; this parser deliberately follows 1.2 so the value the author
  wrote is the value compared.
- Floats: ``1.0`` parses to float 1.0 (distinct from int 1 downstream),
  exponents (``1e3`` -> 1000.0) supported, ``3.10`` -> float 3.1 exactly
  as ``json.loads`` reads ``3.10`` (equivalence with the JSON form of
  the same spec is the contract; no string preservation is invented).
  ``.inf`` / ``-.inf`` / ``.nan`` (any casing) parse to the matching
  Python floats, mirroring PyYAML. JSON cannot express these; in a YAML
  spec they can only appear where a number is grammatically expected
  and LCD compares them as ordinary floats — they are never silently
  stringified or dropped. (Divergence from PyYAML 1.1, deliberate:
  sexagesimal ``1:30`` stays the string ``"1:30"``, and timestamps such
  as ``2026-01-01`` stay STRINGS — no date objects are ever built.)
- Everything else is a string, exactly as written.

DUPLICATE KEYS: rejected at every mapping at every depth (block and
flow), exactly as strict as the JSON path (loads_no_duplicates).
Collision is judged the way Python dict storage judges it: keys in
the int/float/bool family that are == as numbers are the SAME key
(``1:``, ``1.0:`` and ``true:`` are pairwise duplicates; ``-0.0:``
duplicates ``0.0:``), because a Python dict could not hold both.
A string key stays distinct from a numerically equal non-string:
``1:`` (int) and ``'1':`` (str) are distinct keys -> not a duplicate;
``true:`` and ``True:`` are the same bool key -> duplicate.

OUT OF SUBSET — each raises YamlMiniError naming the construct, never
a silent mis-parse:

- anchors (``&a``), aliases (``*a``) and merge keys (``<<``): REJECTED.
  (Chosen over bounded support: OpenAPI generator output does not need
  them, rejection is deterministic, and an alias/"billion laughs" bomb
  is impossible when nothing is ever expanded.)
- custom tags (``!foo``, ``!!str``, ...) -> named error
- directives (``%YAML``, ``%TAG``, ...) -> named error
- explicit mapping keys (``? key``), in block AND flow -> named error
- plain scalars starting with a reserved indicator (``@`` / `````)
  -> named error
- multi-document files (a second ``---`` with content) -> named error
- tab characters in indentation or used as separation -> named error
  (tabs inside block-scalar CONTENT are ordinary content)
- indentation that cannot be resolved deterministically (e.g. a
  mapping-looking line continuing a plain scalar) -> named error
  with the line number

SAFETY LIMITS (all enforced, all fail fast with a limit error):
- document size: MAX_DOCUMENT_BYTES = 32 MiB
- nesting depth: MAX_DEPTH = 200
- total nodes: MAX_NODES = 2,000,000 — every mapping entry (key and
  value), every sequence item and every scalar counts, including
  empty/null values and block scalars, on block, flow and compact
  paths alike (a 10k-path spec is far below)
"""
import re

MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_DEPTH = 200
MAX_NODES = 2_000_000


class YamlMiniError(Exception):
    """YAML-subset parse failure. The message names the construct."""


# ---------------------------------------------------------------- scalars

_INT_RE = re.compile(r"^[-+]?[0-9][0-9_]*$")
_HEX_RE = re.compile(r"^[-+]?0[xX][0-9a-fA-F][0-9a-fA-F_]*$")
_OCT_RE = re.compile(r"^[-+]?0[oO][0-7][0-7_]*$")
_FLOAT_RE = re.compile(
    r"^[-+]?(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)"
    r"(?:[eE][-+]?[0-9]+)?$")


def _check_int_digits(digits, token):
    """Reject integer literals over the interpreter's digit limit
    BEFORE int() can raise a raw ValueError (fix pass item 7a)."""
    if len(digits) > 4300:
        raise YamlMiniError(
            f"integer literal too long ({len(digits)} digits, limit "
            f"4300): {token[:20]!r}...")


def _parse_plain(token):
    """Type a plain (unquoted) scalar token per the module docstring."""
    if token == "" or token in ("null", "Null", "NULL", "~"):
        return None
    if token[0] in "@`":
        raise YamlMiniError(
            f"plain scalar starting with the reserved indicator "
            f"{token[0]!r} is not valid YAML (quote it if it is meant "
            f"as text): {token[:20]!r}")
    if token in ("true", "True", "TRUE"):
        return True
    if token in ("false", "False", "FALSE"):
        return False
    low = token.lower()
    if low in (".inf", "+.inf"):
        return float("inf")
    if low == "-.inf":
        return float("-inf")
    if low == ".nan":
        return float("nan")
    if _HEX_RE.match(token):
        _check_int_digits(token.lstrip("+-")[2:].replace("_", ""), token)
        sign = -1 if token.startswith("-") else 1
        return sign * int(token.lstrip("+-").replace("_", ""), 16)
    if _OCT_RE.match(token):
        _check_int_digits(token.lstrip("+-")[2:].replace("_", ""), token)
        sign = -1 if token.startswith("-") else 1
        return sign * int(token.lstrip("+-")[2:].replace("_", ""), 8)
    if _INT_RE.match(token):
        digits = token.lstrip("+-").replace("_", "")
        _check_int_digits(digits, token)
        if len(digits) > 1 and digits[0] == "0":
            return token  # YAML 1.2: leading-zero decimal is a string
        return int(token.replace("_", ""))
    if _FLOAT_RE.match(token) and ("." in token or "e" in low):
        return float(token.replace("_", ""))
    return token


def _key_id(key):
    """Duplicate-detection identity, aligned with dict storage reality.

    The int/float/bool family shares one identity space keyed by the
    numeric value itself: tuple equality/hash then make 1, 1.0 and
    True collide (and -0.0 collide with 0.0), exactly as they would
    when stored in the result dict. Strings and null keep their own
    spaces, so 1 (number) and '1' (string) stay distinct.
    """
    if isinstance(key, bool) or isinstance(key, (int, float)):
        return ("num", key)
    if isinstance(key, str):
        return ("str", key)
    if key is None:
        return ("null", None)
    raise YamlMiniError(
        f"mapping key must be a scalar, got {type(key).__name__}")


_DQ_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", '"': '"', "/": "/",
    "\\": "\\", "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
    " ": " ",
}


def _fold_quoted_breaks(out, text, j):
    """Handle a run of raw newlines inside a quoted scalar.

    Returns the new scan index. One break folds to a space; n>1 breaks
    become n-1 newlines (YAML quoted-scalar folding).
    """
    breaks = 0
    k = j
    while k < len(text) and text[k] in "\n ":
        if text[k] == "\n":
            breaks += 1
        k += 1
    while out and out[-1] == " ":
        out.pop()
    if k < len(text):  # trailing content exists; a final break is dropped
        if breaks == 1:
            out.append(" ")
        elif breaks > 1:
            out.append("\n" * (breaks - 1))
    return k


def _parse_quoted_at(text, i, lineno):
    """Parse the quoted scalar starting at text[i]; return (value, end)."""
    quote = text[i]
    out = []
    j = i + 1
    if quote == "'":
        while j < len(text):
            c = text[j]
            if c == "'":
                if j + 1 < len(text) and text[j + 1] == "'":
                    out.append("'")
                    j += 2
                    continue
                return "".join(out), j + 1
            if c == "\n":
                j = _fold_quoted_breaks(out, text, j)
                continue
            out.append(c)
            j += 1
        raise YamlMiniError(
            f"unterminated single-quoted scalar starting at line {lineno}")
    # double-quoted
    while j < len(text):
        c = text[j]
        if c == '"':
            return "".join(out), j + 1
        if c == "\n":
            j = _fold_quoted_breaks(out, text, j)
            continue
        if c == "\\":
            if j + 1 >= len(text):
                break
            e = text[j + 1]
            if e == "\n":  # escaped line break: continuation, no output
                j += 2
                while j < len(text) and text[j] in " \t":
                    j += 1
                continue
            if e in ("x", "u", "U"):
                n = {"x": 2, "u": 4, "U": 8}[e]
                hexpart = text[j + 2:j + 2 + n]
                if len(hexpart) != n or \
                        not all(ch in "0123456789abcdefABCDEF" for ch in hexpart):
                    raise YamlMiniError(
                        f"invalid \\{e} escape in double-quoted scalar "
                        f"at line {lineno}")
                codepoint = int(hexpart, 16)
                if codepoint > 0x10FFFF:
                    raise YamlMiniError(
                        f"\\{e} escape in double-quoted scalar at line "
                        f"{lineno} is out of range: U+{codepoint:04X} "
                        f"exceeds the maximum code point U+10FFFF")
                if 0xD800 <= codepoint <= 0xDFFF:
                    raise YamlMiniError(
                        f"\\{e} escape in double-quoted scalar at line "
                        f"{lineno} is a lone surrogate (U+{codepoint:04X}) "
                        f"and is not valid text")
                out.append(chr(codepoint))
                j += 2 + n
                continue
            if e in _DQ_ESCAPES:
                out.append(_DQ_ESCAPES[e])
                j += 2
                continue
            raise YamlMiniError(
                f"unknown escape sequence \\{e} in double-quoted scalar "
                f"at line {lineno}")
        out.append(c)
        j += 1
    raise YamlMiniError(
        f"unterminated double-quoted scalar starting at line {lineno}")


class _FlowScanner:
    """Incremental flow-completeness scanner (fix pass item 5).

    The pre-fix code re-scanned the whole accumulated text on every
    appended line — O(n^2) on multi-line flow collections. This
    scanner carries its state (bracket depth, quote, escape, comment)
    across chunks and only ever scans the newly appended chunk, so
    collecting a flow collection is O(total input size).
    """

    def __init__(self, lineno):
        self.lineno = lineno
        self.depth = 0
        self.quote = None       # None, "'" or '"'
        self.esc = False        # next char is escaped (double quotes)
        self.pending_sq = False  # a "'" that may be a '' pair or close
        self.in_comment = False
        self.prev = None        # last raw character seen

    @property
    def complete(self):
        # A pending single quote at end-of-text counts as closed (the
        # old whole-text scanner treated a final "'" as closing).
        return self.depth <= 0 and (self.quote is None or self.pending_sq)

    def feed(self, chunk):
        for c in chunk:
            self._step(c)
            self.prev = c

    def _step(self, c):
        if self.in_comment:
            if c == "\n":
                self.in_comment = False
            return
        if self.quote == "'":
            if self.pending_sq:
                if c == "'":
                    self.pending_sq = False
                    return
                self.quote = None
                self.pending_sq = False
                # fall through: c is processed in normal mode
            else:
                if c == "'":
                    self.pending_sq = True
                return
        if self.quote == '"':
            if self.esc:
                self.esc = False
                return
            if c == "\\":
                self.esc = True
                return
            if c == '"':
                self.quote = None
            return
        # normal (unquoted) mode
        if c in "'\"" and (self.prev is None or
                           self.prev in " \t\n,[]{}:"):
            self.quote = c
        elif c == "#" and (self.prev is None or
                           self.prev in " \t\n,[]{}:"):
            self.in_comment = True
        elif c in "[{":
            self.depth += 1
        elif c in "]}":
            self.depth -= 1
            if self.depth < 0:
                raise YamlMiniError(
                    f"unbalanced flow collection at line {self.lineno}")


class _QuoteScanner:
    """Incremental terminated-ness scanner for one quoted scalar.

    Same O(n^2) -> O(n) restructuring as _FlowScanner: only the newly
    appended chunk is scanned, never the accumulated text.
    """

    def __init__(self, quote):
        self.quote = quote
        self.esc = False
        self.pending_sq = False
        self.closed = False

    def feed(self, chunk, start=0):
        for c in chunk[start:]:
            if self.closed:
                return
            if self.quote == "'":
                if self.pending_sq:
                    if c == "'":
                        self.pending_sq = False
                        continue
                    self.closed = True
                    return
                if c == "'":
                    self.pending_sq = True
                continue
            # double-quoted
            if self.esc:
                self.esc = False
                continue
            if c == "\\":
                self.esc = True
                continue
            if c == '"':
                self.closed = True
                return


def _cut_plain_comment(text):
    """Cut a plain fragment at comment position (' #' or a leading '#')."""
    for i, c in enumerate(text):
        if c == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
    return text


# ------------------------------------------------------------------- flow

class _FlowParser:
    """Recursive-descent parser for one flow node (possibly multi-line)."""

    def __init__(self, text, owner, lineno, counting=True):
        self.text = text
        self.owner = owner
        self.lineno = lineno
        self.pos = 0
        # Probe parses (from _split_key) must not burn the node budget:
        # counting reflects the document, not the probing (item 6).
        self.counting = counting

    def _err(self, msg):
        return YamlMiniError(f"{msg} (flow collection at line {self.lineno})")

    def _skip(self):
        t, n = self.text, len(self.text)
        while self.pos < n:
            c = t[self.pos]
            if c in " \t\n":
                self.pos += 1
            elif c == "#" and (self.pos == 0 or
                               t[self.pos - 1] in " \t\n,[]{}:"):
                while self.pos < n and t[self.pos] != "\n":
                    self.pos += 1
            else:
                break

    def _peek_char(self):
        self._skip()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def parse_node(self, depth):
        """Parse one flow node; `depth` = enclosing collections count."""
        if self.counting:
            self.owner._count()
        self._skip()
        if self.pos >= len(self.text):
            raise self._err("unterminated flow collection")
        c = self.text[self.pos]
        if c == "?" and (self.pos + 1 >= len(self.text) or
                         self.text[self.pos + 1] in " \t\n"):
            raise YamlMiniError(
                f"explicit mapping keys (?) are not supported "
                f"(line {self.lineno})")
        if c in "{[":
            if depth + 1 > MAX_DEPTH:
                raise self._err(
                    f"YAML nesting exceeds the maximum depth of "
                    f"{MAX_DEPTH}")
            return self._parse_map(depth) if c == "{" \
                else self._parse_seq(depth)
        if c in "'\"":
            val, end = _parse_quoted_at(self.text, self.pos, self.lineno)
            self.pos = end
            return val
        if c in "&*!":
            raise self.owner._forbidden(self.text[self.pos:], self.lineno)
        if c in "|>":
            raise self._err("block scalars are not allowed in flow collections")
        return self._parse_plain_flow()

    def _parse_plain_flow(self):
        t, start = self.text, self.pos
        while self.pos < len(t):
            c = t[self.pos]
            if c in ",]}":
                break
            if c == ":" and (self.pos + 1 >= len(t) or
                             t[self.pos + 1] in ",]} \t\n"):
                break
            if c == "#" and self.pos > start and t[self.pos - 1] in " \t":
                break
            if c in "[{":
                raise self._err(
                    f"unexpected {c!r} inside a plain flow scalar")
            self.pos += 1
        token = t[start:self.pos].strip()
        if token == "":
            raise self._err("empty scalar in flow collection")
        if token == "<<":
            raise self.owner._merge_key_error(self.lineno)
        # Plain scalars fold across lines: one break -> a space, n
        # breaks -> n-1 newlines (same rule as block plain scalars).
        if "\n" in token:
            token = re.sub(r"(?:[ \t]*\n[ \t]*)+",
                           lambda m: " " if m.group(0).count("\n") == 1
                           else "\n" * (m.group(0).count("\n") - 1), token)
        return _parse_plain(token)

    def _parse_map(self, depth):
        own = depth + 1  # this collection's own nesting level
        self.pos += 1  # {
        result, seen = {}, {}
        if self._peek_char() == "}":
            self.pos += 1
            return result
        while True:
            key = self.parse_node(own)
            kid = _key_id(key)
            if kid in seen:
                raise self._err(
                    f"duplicate key {key!r} in flow mapping "
                    f"(first seen at line {seen[kid]})")
            seen[kid] = self.lineno
            if self._peek_char() == ":":
                self.pos += 1
                nxt = self._peek_char()
                if nxt in ",}":
                    value = None
                    if self.counting:
                        self.owner._count()  # the implicit null value
                else:
                    value = self.parse_node(own)
            else:
                value = None  # {key} means {key: null}
                if self.counting:
                    self.owner._count()  # the implicit null value
            result[key] = value
            c = self._peek_char()
            if c == ",":
                self.pos += 1
                if self._peek_char() == "}":
                    self.pos += 1
                    return result
                continue
            if c == "}":
                self.pos += 1
                return result
            raise self._err("expected ',' or '}' in flow mapping")

    def _parse_seq(self, depth):
        own = depth + 1  # this collection's own nesting level
        self.pos += 1  # [
        result = []
        if self._peek_char() == "]":
            self.pos += 1
            return result
        while True:
            if self._peek_char() == ",":
                raise self._err("empty entry in flow sequence")
            result.append(self.parse_node(own))
            c = self._peek_char()
            if c == ",":
                self.pos += 1
                if self._peek_char() == "]":
                    self.pos += 1
                    return result
                continue
            if c == "]":
                self.pos += 1
                return result
            raise self._err("expected ',' or ']' in flow sequence")

    def expect_end(self):
        self._skip()
        if self.pos < len(self.text):
            raise self._err(
                f"unexpected content after flow collection: "
                f"{self.text[self.pos:self.pos + 20]!r}")


# ------------------------------------------------------------------ block

_BLOCK_HEADER_RE = re.compile(r"^([|>])([+-]?)([1-9]?)([+-]?)[ \t]*(#.*)?$")


class _Parser:
    def __init__(self, text):
        if not isinstance(text, str):
            raise YamlMiniError("YAML input must be text")
        nbytes = len(text.encode("utf-8", "replace"))
        if nbytes > MAX_DOCUMENT_BYTES:
            raise YamlMiniError(
                f"YAML document exceeds the maximum size of "
                f"{MAX_DOCUMENT_BYTES} bytes (got {nbytes})")
        if text.startswith("\ufeff"):
            text = text[1:]
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # YAML line breaks include U+2028, U+2029 and NEL (U+0085):
        # normalise them like CR so they split lines in every context
        # instead of being silently swallowed into scalars (item 8).
        # (The \N / \L / \P escapes produce these characters during
        # scalar parsing, after splitting, and are unaffected.)
        text = text.replace("\u2028", "\n").replace("\u2029", "\n") \
                   .replace("\x85", "\n")
        self._ends_with_newline = text.endswith("\n")
        self.lines = text.split("\n")
        if len(self.lines) > 1 and self.lines[-1] == "":
            # split() artefact: a trailing newline terminates the last
            # line, it does not start a new (phantom) one.
            self.lines.pop()
        self.pos = 0
        self.nodes = 0
        self._split_cache = {}
        # NOTE: there is deliberately no global tab-in-indentation
        # pre-scan any more: block-scalar content lines are consumed
        # without passing through _peek and may legitimately contain
        # tabs (item 12). The tab check lives in _peek, which every
        # STRUCTURAL line passes through.

    # -- bookkeeping -------------------------------------------------
    def _count(self):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise YamlMiniError(
                f"YAML document exceeds the maximum of {MAX_NODES} nodes")

    def _forbidden(self, frag, lineno):
        c = frag[0]
        token = frag.split(None, 1)[0] if frag.split() else frag
        if c == "&":
            return YamlMiniError(
                f"YAML anchors ({token}) are not supported "
                f"(line {lineno}); write the value out in full")
        if c == "*":
            return YamlMiniError(
                f"YAML aliases ({token}) are not supported "
                f"(line {lineno}); write the value out in full")
        return YamlMiniError(
            f"YAML tags ({token}) are not supported (line {lineno})")

    def _merge_key_error(self, lineno):
        return YamlMiniError(
            f"YAML merge keys (<<) are not supported (line {lineno}); "
            f"write the merged entries out in full")

    # -- line access ---------------------------------------------------
    def _peek(self):
        """Next structural line: (index, indent, content) or None."""
        i = self.pos
        while i < len(self.lines):
            raw = self.lines[i]
            s = raw.strip()
            if s == "":
                i += 1
                continue
            lead = raw[:len(raw) - len(raw.lstrip(" \t"))]
            if "\t" in lead:
                raise YamlMiniError(
                    f"tab character in indentation at line {i + 1} is "
                    f"not supported; indent with spaces")
            if s.startswith("#"):
                i += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            return i, indent, raw[indent:]
        return None

    def _take(self):
        p = self._peek()
        if p is not None:
            self.pos = p[0] + 1
        return p

    @staticmethod
    def _is_marker(content, marker):
        c = content.rstrip()
        return c == marker or c.startswith(marker + " ") or \
            c.startswith(marker + "\t")

    @staticmethod
    def _is_dash(content):
        return content == "-" or content.startswith("- ") or \
            content.startswith("-\t")

    # -- key splitting -------------------------------------------------
    def _split_key(self, content, lineno):
        """Memoised wrapper: a line is probed several times by the
        block parsers; the split (including any flow-key probe parse)
        is computed once per (line, content) so neither time nor the
        node budget is spent on re-probing (fix pass item 6)."""
        cache_key = (lineno, content)
        if cache_key in self._split_cache:
            return self._split_cache[cache_key]
        result = self._split_key_uncached(content, lineno)
        self._split_cache[cache_key] = result
        return result

    def _split_key_uncached(self, content, lineno):
        """Split a block mapping line into (key, rest_after_colon).

        Returns None when the line is not a mapping entry. Raises
        YamlMiniError for constructs that look like keys but are out of
        the supported subset.
        """
        if not content:
            return None
        c0 = content[0]
        if c0 in "&*!":
            raise self._forbidden(content, lineno)
        if c0 == "?" and (len(content) == 1 or content[1] in " \t"):
            raise YamlMiniError(
                f"explicit mapping keys (?) are not supported "
                f"(line {lineno})")
        if c0 in "'\"":
            try:
                key, end = _parse_quoted_at(content, 0, lineno)
            except YamlMiniError:
                return None  # a quoted scalar value, not a key
            j = end
            saw_tab = False
            while j < len(content) and content[j] in " \t":
                saw_tab = saw_tab or content[j] == "\t"
                j += 1
            if j < len(content) and content[j] == ":" and \
                    (j + 1 >= len(content) or content[j + 1] in " \t"):
                if saw_tab:
                    raise YamlMiniError(
                        f"tab character used as separation at line "
                        f"{lineno} is not supported; use spaces")
                return key, content[j + 1:]
            return None
        if c0 in "[{":
            fp = _FlowParser(content, self, lineno, counting=False)
            try:
                key = fp.parse_node(0)
            except YamlMiniError:
                return None
            fp._skip()
            if fp.pos < len(content) and content[fp.pos] == ":":
                return key, content[fp.pos + 1:]
            return None
        # plain key: scan for ':' followed by space/EOL at flow depth 0,
        # outside quoted regions (a quote only opens a region where a
        # scalar could start: after start/space/colon/comma/bracket).
        depth = 0
        quote = None
        i = 0
        while i < len(content):
            c = content[i]
            if quote == "'":
                if c == "'":
                    if i + 1 < len(content) and content[i + 1] == "'":
                        i += 2
                        continue
                    quote = None
                i += 1
                continue
            if quote == '"':
                if c == "\\":
                    i += 2
                    continue
                if c == '"':
                    quote = None
                i += 1
                continue
            if c in "'\"" and (i == 0 or content[i - 1] in " \t:[{,"):
                quote = c
                i += 1
                continue
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            elif c == "#" and depth == 0 and i > 0 and \
                    content[i - 1] in " \t":
                return None  # comment: no key on this line
            elif c == ":" and depth == 0 and \
                    (i + 1 >= len(content) or content[i + 1] in " \t"):
                if i > 0 and content[i - 1] == "\t":
                    raise YamlMiniError(
                        f"tab character used as separation at line "
                        f"{lineno} is not supported; use spaces")
                key_text = content[:i].strip()
                if key_text == "":
                    return None
                if key_text == "<<":
                    raise self._merge_key_error(lineno)
                return _parse_plain(key_text), content[i + 1:]
            i += 1
        return None

    def _check_directives(self):
        """Directives (%YAML/%TAG) precede the document; name them
        instead of misreporting them as a second document (item 11a)."""
        for i, raw in enumerate(self.lines):
            s = raw.strip()
            if s == "" or s.startswith("#"):
                continue
            if raw.startswith("%"):
                token = raw.split(None, 1)[0] if raw.split() else "%"
                raise YamlMiniError(
                    f"YAML directives ({token}) are not supported "
                    f"(line {i + 1}); remove the directive line")
            break

    def _validate_end_marker(self, content, idx):
        """'...' may be followed only by a comment; trailing junk on
        the marker line itself must not be silently dropped (11b)."""
        rest = content.rstrip()[3:].strip()
        if rest and not rest.startswith("#"):
            raise YamlMiniError(
                f"unexpected content after the '...' document-end "
                f"marker at line {idx + 1}: {rest[:30]!r}")

    # -- document ------------------------------------------------------
    def parse_document(self):
        self._check_directives()
        p = self._peek()
        if p is None:
            return None
        idx, indent, content = p
        if self._is_marker(content, "---"):
            if content.rstrip() == "---":
                self.pos = idx + 1
                p = self._peek()
                if p is None:
                    return None
                idx, indent, content = p
            else:  # '--- <node>' on one line
                rest = content[3:].lstrip(" \t")
                self.lines[idx] = " " * (indent + 4) + rest
                idx, indent, content = self._peek()
        if self._is_marker(content, "..."):
            self._validate_end_marker(content, idx)
            self.pos = idx + 1
            self._check_trailing(after_end=True)
            return None
        node = self._parse_block(indent, 0)
        p = self._peek()
        if p is not None:
            idx, indent, content = p
            if self._is_marker(content, "..."):
                self._validate_end_marker(content, idx)
                self.pos = idx + 1
                self._check_trailing(after_end=True)
            elif self._is_marker(content, "---"):
                raise YamlMiniError(
                    f"multiple YAML documents are not supported: second "
                    f"'---' document marker at line {idx + 1}")
            else:
                raise YamlMiniError(
                    f"unexpected content at line {idx + 1} (inconsistent "
                    f"indentation?): {content.strip()[:40]!r}")
        return node

    def _check_trailing(self, after_end=False):
        p = self._peek()
        if p is None:
            return
        idx, indent, content = p
        if self._is_marker(content, "---"):
            raise YamlMiniError(
                f"multiple YAML documents are not supported: second "
                f"'---' document marker at line {idx + 1}")
        raise YamlMiniError(
            f"unexpected content after document end ('...') at line "
            f"{idx + 1}: {content.strip()[:40]!r}")

    # -- block structures ------------------------------------------------
    # Depth convention (fix pass item 14): every depth parameter is
    # the number of ENCLOSING collections. A collection's own level is
    # enclosing+1; levels 1..MAX_DEPTH are allowed in block and flow
    # syntax alike, and creating a collection at level MAX_DEPTH+1 is
    # the depth error. A scalar leaf inside MAX_DEPTH collections is
    # always fine.
    def _parse_block(self, indent, depth):
        p = self._peek()
        if p is None:
            return None
        idx, ind, content = p
        if self._is_dash(content):
            return self._parse_sequence(ind, depth)
        if self._split_key(content, idx + 1) is not None:
            return self._parse_mapping(ind, depth)
        self._take()
        return self._parse_inline(content, ind, idx + 1, depth)

    def _parse_mapping(self, indent, depth, first=None):
        if depth + 1 > MAX_DEPTH:
            raise YamlMiniError(
                f"YAML nesting exceeds the maximum depth of {MAX_DEPTH}")
        own = depth + 1
        self._count()
        result, seen = {}, {}
        pending = first
        while True:
            if pending is not None:
                lineno, content = pending
                pending = None
            else:
                p = self._peek()
                if p is None:
                    break
                idx, ind, content = p
                if ind != indent:
                    break
                if self._is_marker(content, "---") or \
                        self._is_marker(content, "..."):
                    break
                if self._is_dash(content):
                    break
                if self._split_key(content, idx + 1) is None:
                    break
                self.pos = idx + 1
                lineno = idx + 1
            split = self._split_key(content, lineno)
            if split is None:
                raise YamlMiniError(
                    f"could not parse a mapping entry at line {lineno}: "
                    f"{content.strip()[:40]!r}")
            key, rest = split
            self._count()  # the entry's key node (item 6)
            try:
                kid = _key_id(key)
            except YamlMiniError as e:
                raise YamlMiniError(f"{e} (line {lineno})")
            if kid in seen:
                raise YamlMiniError(
                    f"duplicate key {key!r} at line {lineno} "
                    f"(first defined at line {seen[kid]})")
            seen[kid] = lineno
            result[key] = self._parse_value_or_empty(rest, indent,
                                                     lineno, own)
        return result

    def _parse_sequence(self, indent, depth, first=None):
        if depth + 1 > MAX_DEPTH:
            raise YamlMiniError(
                f"YAML nesting exceeds the maximum depth of {MAX_DEPTH}")
        own = depth + 1
        self._count()
        result = []
        pending = first
        while True:
            if pending is not None:
                lineno, content = pending
                pending = None
            else:
                p = self._peek()
                if p is None:
                    break
                idx, ind, content = p
                if ind != indent or not self._is_dash(content):
                    break
                self.pos = idx + 1
                lineno = idx + 1
            after = content[1:]
            after_spaces = after.lstrip(" ")
            if after_spaces.startswith("\t") and \
                    after_spaces.strip() != "":
                raise YamlMiniError(
                    f"tab character used as separation at line "
                    f"{lineno} is not supported; use spaces")
            frag = after.lstrip(" \t")
            spaces = len(after) - len(after.lstrip(" "))
            item_col = indent + 1 + max(spaces, 1)
            if frag == "" or frag.startswith("#"):
                value = self._empty_seq_value(indent, own)
            elif self._is_dash(frag):
                # nested sequence FIRST: '- - key: v' is a sequence in a
                # sequence whose item is a mapping, never a mapping with
                # a '- - key' key.
                value = self._parse_sequence(item_col, own,
                                             first=(lineno, frag))
            elif self._split_key(frag, lineno) is not None:
                value = self._parse_mapping(item_col, own,
                                            first=(lineno, frag))
            else:
                value = self._parse_inline(frag, indent, lineno, own)
            result.append(value)
        return result

    def _empty_value(self, parent_indent, depth):
        """Value of a 'key:' with nothing after it on the line.

        `depth` is the enclosing-collections count for the value (the
        owning mapping's own level). The indentless-sequence rule
        (a sequence at the SAME indent as the key) applies here and
        ONLY here — see _empty_seq_value for sequence items.
        """
        p = self._peek()
        if p is None:
            self._count()  # the null value node (item 6)
            return None
        idx, ind, content = p
        if self._is_marker(content, "---") or self._is_marker(content, "..."):
            self._count()
            return None
        if ind > parent_indent:
            return self._parse_block(ind, depth)
        if ind == parent_indent and self._is_dash(content):
            return self._parse_sequence(ind, depth)
        self._count()
        return None

    def _empty_seq_value(self, seq_indent, depth):
        """Value of a '-' sequence item with nothing after it.

        Fix pass item 1: unlike a mapping value, an empty sequence
        item's value can only be a MORE-indented block on the
        following lines. A dash at the SAME indent is the next
        sibling item, never a child — routing it to _empty_value
        used to swallow every following sibling into a spurious
        nested sequence (and burned a depth level per sibling).
        """
        p = self._peek()
        if p is None:
            self._count()  # the null item node (item 6)
            return None
        idx, ind, content = p
        if self._is_marker(content, "---") or self._is_marker(content, "..."):
            self._count()
            return None
        if ind > seq_indent:
            return self._parse_block(ind, depth)
        self._count()
        return None

    def _parse_value_or_empty(self, rest, indent, lineno, depth):
        after_colon = rest.lstrip(" ")
        if after_colon.startswith("\t") and after_colon.strip() != "":
            raise YamlMiniError(
                f"tab character used as separation at line {lineno} "
                f"is not supported; use spaces")
        frag = rest.strip()
        if frag == "" or frag.startswith("#"):
            return self._empty_value(indent, depth)
        return self._parse_inline(rest, indent, lineno, depth)

    @staticmethod
    def _check_plain_colon(token, lineno):
        """Reject a block plain scalar containing a mapping indicator
        (fix pass item 10): ': ' / ':\\t' inside the scalar, or a
        trailing ':', would form a nested mapping token — PyYAML
        errors on all of these ('mapping values are not allowed
        here'). Colons NOT followed by separation (URLs, 12:34:56)
        are unaffected, as are quoted scalars (never checked)."""
        if ": " in token or ":\t" in token or token.endswith(":"):
            raise YamlMiniError(
                f"mapping values are not allowed here: the plain "
                f"scalar at line {lineno} contains a ': ' mapping "
                f"indicator (quote the value if it is meant as text)")

    # -- inline values ---------------------------------------------------
    def _parse_inline(self, frag, parent_indent, lineno, depth):
        # `depth` = enclosing collections; collections created below
        # check their own level against MAX_DEPTH (item 14).
        f = frag.lstrip(" \t")
        if f == "" or f.startswith("#"):
            self._count()  # a null scalar node (item 6)
            return None
        c = f[0]
        if c in "&*!":
            raise self._forbidden(f, lineno)
        if c in "|>":
            self._count()  # the block scalar node (item 6)
            return self._parse_block_scalar(f, parent_indent, lineno)
        if c in "[{":
            # No count here: _FlowParser.parse_node counts the
            # collection node itself (counting it in both places
            # double-counted every block-level flow value).
            text = self._collect_flow(f, lineno)
            fp = _FlowParser(text, self, lineno)
            node = fp.parse_node(depth)
            fp.expect_end()
            return node
        if c in "'\"":
            self._count()
            text = self._collect_quoted(f)
            val, end = _parse_quoted_at(text, 0, lineno)
            rest = text[end:]
            # A comment may follow a quoted scalar in block context
            # even glued directly to the closing quote (item 13);
            # flow context is unchanged (the flow parser rejects it,
            # as does PyYAML).
            if rest.strip() != "" and not rest.strip().startswith("#"):
                raise YamlMiniError(
                    f"unexpected content after quoted scalar at line "
                    f"{lineno}: {rest.strip()[:30]!r}")
            return val
        # plain scalar, possibly spanning more-indented lines
        self._count()
        token = _cut_plain_comment(f).strip()
        if token == "<<":
            raise self._merge_key_error(lineno)
        self._check_plain_colon(token, lineno)
        folded = self._fold_plain(token, parent_indent)
        return _parse_plain(folded)

    def _collect_quoted(self, first_frag):
        # Incremental scan (item 5): only each newly appended line is
        # scanned for the closing quote, never the accumulated text.
        scanner = _QuoteScanner(first_frag[0])
        scanner.feed(first_frag, start=1)
        text = first_frag
        # A pending single quote at a chunk boundary counts as closed:
        # the next chunk always starts with "\n", and '' pairs only
        # form between ADJACENT quotes, so the pending quote cannot be
        # half of a pair across the boundary.
        while not (scanner.closed or scanner.pending_sq):
            if self.pos >= len(self.lines):
                break  # _parse_quoted_at raises the unterminated error
            chunk = "\n" + self.lines[self.pos]
            self.pos += 1
            text += chunk
            scanner.feed(chunk)
        return text

    def _collect_flow(self, first_frag, lineno):
        # Incremental scan (item 5): scanner state is carried across
        # lines; collecting is O(total size), not O(lines * size).
        scanner = _FlowScanner(lineno)
        scanner.feed(first_frag)
        text = first_frag
        while not scanner.complete:
            if self.pos >= len(self.lines):
                break  # the flow parser raises the unterminated error
            chunk = "\n" + self.lines[self.pos]
            self.pos += 1
            text += chunk
            scanner.feed(chunk)
        return text

    def _fold_plain(self, first, base_indent):
        """Fold more-indented continuation lines into a plain scalar."""
        parts = [first]
        scan = self.pos
        blanks = 0
        while True:
            j = scan
            nblanks = blanks
            while j < len(self.lines):
                s = self.lines[j].strip()
                if s == "":
                    nblanks += 1
                    j += 1
                elif s.startswith("#"):
                    j += 1
                else:
                    break
            if j >= len(self.lines):
                break
            raw = self.lines[j]
            lead = raw[:len(raw) - len(raw.lstrip(" \t"))]
            if "\t" in lead:
                raise YamlMiniError(
                    f"tab character in indentation at line {j + 1} is "
                    f"not supported; indent with spaces")
            ind = len(raw) - len(raw.lstrip(" "))
            if ind <= base_indent:
                break
            content = raw[ind:].rstrip()
            if self._split_key(content, j + 1) is not None:
                raise YamlMiniError(
                    f"mapping values are not allowed here: line {j + 1} "
                    f"looks like a mapping entry but continues the plain "
                    f"scalar from an earlier line (inconsistent "
                    f"indentation)")
            text = _cut_plain_comment(content).strip()
            if text == "":
                scan = j + 1
                continue
            self._check_plain_colon(text, j + 1)
            sep = " " if nblanks == 0 else "\n" * nblanks
            parts.append(sep + text)
            scan = j + 1
            self.pos = scan
            blanks = 0
        return "".join(parts)

    # -- block scalars ---------------------------------------------------
    def _parse_block_scalar(self, header, parent_indent, lineno):
        m = _BLOCK_HEADER_RE.match(header.strip())
        if not m:
            raise YamlMiniError(
                f"invalid block scalar header {header.strip()!r} at line "
                f"{lineno}")
        style = m.group(1)
        chomping = m.group(2) or m.group(4) or ""
        digit = m.group(3)
        # collect raw content lines: blank lines or lines indented past
        # the parent; stop at the first structural line at/below parent.
        gathered = []  # (text_after_block_indent or None for blank, more)
        scan = self.pos
        explicit = int(digit) if digit else None
        block_indent = parent_indent + explicit if explicit else None
        while scan < len(self.lines):
            raw = self.lines[scan]
            if raw.strip() == "":
                gathered.append(None)
                scan += 1
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            if ind <= parent_indent:
                break
            if block_indent is None:
                block_indent = ind
            if ind < block_indent:
                raise YamlMiniError(
                    f"inconsistent indentation in block scalar at line "
                    f"{scan + 1}: line is indented {ind} spaces, the "
                    f"block is indented {block_indent}")
            text_after_indent = raw[block_indent:]
            # More-indented for folding purposes: extra spaces beyond
            # the block indent, or content starting with a tab once
            # the indent is reached (PyYAML preserves the break
            # before such a line instead of folding it — item 12).
            gathered.append((text_after_indent,
                             text_after_indent[:1] in (" ", "\t")))
            scan += 1
        self.pos = scan
        # trailing blank lines belong to the chomping calculation
        trailing = 0
        while gathered and gathered[-1] is None:
            gathered.pop()
            trailing += 1
        body_lines = [("" if g is None else g[0],
                       False if g is None else g[1]) for g in gathered]
        if not body_lines:
            # A scalar of ONLY blank lines (fix pass item 11c): keep
            # chomping preserves one newline per blank line; clip and
            # strip both yield the empty string (PyYAML-verified).
            # (The pre-fix code returned '' for every chomping mode.)
            if chomping == "+":
                return "\n" * trailing
            return ""
        if style == "|":
            body = "\n".join(t for t, _ in body_lines)
        else:  # folded '>', per YAML 1.2 §8.1.3 (fix pass item 4):
            # - a break between two non-blank lines folds to a space,
            #   unless either side is more-indented -> kept as "\n";
            # - more-indented lines are never folded;
            # - a run of k blank lines between content lines becomes
            #   k newlines, or k+1 when either neighbour is
            #   more-indented (the breaks around a more-indented line
            #   are preserved, they do not fold into the blank run);
            # - k leading blank lines become k newlines.
            # (The pre-fix paragraph code dropped one newline whenever
            # a blank line sat next to a more-indented line.)
            segments = []  # (text, more_indented, blanks_before)
            blanks = 0
            leading = None
            for t, more in body_lines:
                if t == "":
                    blanks += 1
                    continue
                if leading is None:
                    leading = blanks
                segments.append((t, more, blanks))
                blanks = 0
            out = ["\n" * (leading or 0)]
            for si, (t, more, k) in enumerate(segments):
                if si > 0:
                    prev_more = segments[si - 1][1]
                    if k == 0:
                        out.append("\n" if (more or prev_more) else " ")
                    elif more or prev_more:
                        out.append("\n" * (k + 1))
                    else:
                        out.append("\n" * k)
                out.append(t)
            body = "".join(out)
        # Line breaks after the last content line: its own break plus
        # one per trailing blank line — minus the break that does not
        # exist when the file ends mid-line at EOF.
        if scan < len(self.lines) or self._ends_with_newline:
            breaks = 1 + trailing
        else:
            breaks = trailing
        if body_lines:
            body += "\n" * breaks
        if chomping == "-":
            body = body.rstrip("\n")
        elif chomping == "+":
            pass  # keep everything
        else:  # clip: exactly one trailing newline, if there was a break
            body = body.rstrip("\n") + ("\n" if body_lines and breaks else "")
        return body


def loads(text):
    """Parse YAML-subset text into Python structures (see module docs).

    Raises YamlMiniError on any unsupported construct, duplicate key,
    malformed input or exceeded safety limit.
    """
    try:
        return _Parser(text).parse_document()
    except RecursionError:
        # The interpreter's own stack limit can trip just before
        # MAX_DEPTH on very deep documents; it is the same limit
        # failure, reported the same way — never a crash.
        raise YamlMiniError(
            f"YAML nesting exceeds the maximum depth of {MAX_DEPTH}")
