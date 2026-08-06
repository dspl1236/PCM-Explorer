"""DCL -- the PCM's diagnostic description format (``diagnosis.cfg``).

PCM3Reload does not implement KWP services in code.  Its classes are interpreter
nodes -- ``CRBDiagService``, ``CRBDiagCalculate``, ``CRBDiagConst``,
``CRBDiagTable``, ``CRBDiagSelect`` -- each with an ``onDispatch``, walking a
description.  This is that description.

## Container

The file is an ASCII header followed by **one flat, gapless record stream** that
runs to EOF.  There is no padding, no resync and no per-section framing::

    file      := header preamble stream
    header    := hdrstr*                      # ends when flag is not 0 or 1
    hdrstr    := u16 flag ; u16 len ; byte[len] ; (u8 NUL if flag == 1)
    preamble  := 02 00 03 00
    stream    := record*
    record    := u16 code ; body(code)

The leading ``u16 code`` is the whole discriminator: body length is a function
of the code alone, plus an inline count for the five variable-length codes.
There is **no explicit length field**.  Every earlier attempt at this format
assumed one -- ``u8 kind | u16 ident | u8 tag | u16 type | u16 len`` -- and the
bytes that looked like the length were the payload's own first ``u16``.  That
one mistake is why a flat walk kept stalling around 50-74%, and it is also why
the file looked sectioned: a walk that desynchronises resynchronises on the
next ASCII string, and the only ASCII in the body is the entry names.

Codes 1-3 are *path* records opening an attribute; every record after one, up
to the next path record, is a value belonging to it.  A path element is
``u8 slot ; u16 ident ; u8 cls``, and ``cls & 0x80`` means another element
follows -- redundant with the code, and never once inconsistent in 10846
records.

    slot   2 + 4*n, n in 0..8.  Which attribute of the object this is.
    cls    the object's class/scope: 01 03 04 05 06 07 08 09 0e 21 50 5a.
    ident  the object id inside that scope.  Sparse and non-contiguous.

The stream is column-major: within one ``(cls, slot)`` run the idents ascend,
then the next slot restarts low.

## Sections are logical, not physical

The header declares seven entries -- ``Protokoll``, ``CommProcessor``,
``DataProc``, ``HBProduction``, ``ODXCheck``, ``SH4Internal``, ``Version`` --
and six of them recur in the body.  They are **not markers**.  Each body
occurrence is the ASCII payload of an ordinary ``0x2a`` string value hanging off
a normal path record, e.g. in gen400 at 0x012851::

    01 00 | 02 47 05 07 | 2a 00 | 0b 00 | "SH4Internal"

``Protokoll`` never appears in the body at all, in any of the five generations,
which by itself kills the seven-section model.  The names are ``NextLayer``
dispatch targets: every one is the value of an attribute in class ``0x01``, the
Protokoll root layer.  Nothing about the framing changes at a name, so
``records()`` needs no per-section dispatch and ``coverage()`` reports the same
100% in every range.  ``section_ranges()`` exists because the byte extents are
still the useful way to say *where* something lives.

## Expressions and their operands

Code ``0x23`` carries an RPN expression as an ASCII operator template, with
``x`` standing for an operand.  Operands are code-7 records: ``u16 src,
srcport, dst, dstport, seq ; u32 const ; u8 isconst ; u8 opindex``.  ``isconst``
says whether the value is the literal in ``const`` or arrives over the dataflow
graph from ``src:srcport``.

They group **positionally**, by the attribute that owns them -- see
:func:`operands`.  There is no pointer from a ``0x23`` record to its operand
group; matching is on arity.

**The calculate nodes form a dataflow graph, and most expressions cannot be
evaluated standalone.**  Both nine-member operand groups in gen400 have
``isconst = 0`` on every row: all nine inputs arrive from other nodes
(``src`` 230-238 and 743-751 respectively), with no constants at all.  So
evaluating an expression means resolving its upstream nodes first, and feeding
a value directly into a placeholder is not a meaningful operation.

## Status

**The container format is solved.  The semantic layer above it is not.**

``records()`` explains 100.00% of the stream in all five distinct shipped
variants -- the fifteen files dedupe to five by sha256 -- landing exactly on
EOF with zero tail and zero skipped bytes, and no resync path exists.  It
survives the tests that catch overfitting: 27 of 65536 u16 values are legal
codes, random data runs 0 bytes, 62 of 64 wrong start offsets die within two
records, and single-byte corruption breaks the walk at 15.7% against a 14.5%
code-byte fraction.  Three invariants the parser never reads -- path element
count vs code, the ``cls & 0x80`` continuation bit, inner string length -- hold
across all 10846 records with zero exceptions, so they cannot have been fitted.

The record walk recovers every expression the string oracle finds and three
more (``"xa'"`` twice and ``"xA'"`` in gen400) that the oracle's character
heuristic misses.  A superset, not an identical set: the walk is the more
correct of the two.

What is **not** established: what any expression computes.  100% coverage
proves record *boundaries*, not field *interpretation* -- 85.5% of the stream
is payload the walk never inspects.  In particular there is no support here for
any claim about the nine-placeholder expression, and no evidence either way on
whether the KWP SecurityAccess seed/key derivation lives in this file.

``scan_expressions()`` stays independent of the record model on purpose: it is
the oracle the walk is scored against.  Coverage alone is not evidence and must
never be reported as if it were -- a dynamic-programming walk free to choose
unknown payload sizes reached 99.49% coverage while recovering 17 of 49
expressions.
"""
import struct
from collections import defaultdict

#: Body size per record code.  Ints are fixed sizes; the strings are the five
#: self-describing shapes resolved by :func:`body_len`.  Every entry was
#: derived by contiguous hand-decode and then confirmed by the walk landing
#: exactly on EOF in all fifteen files.
CODE_SIZE = {
    1: 4, 2: 8, 3: 12,          # path, 1/2/3 elements
    4: 2,
    6: 10,                      # dataflow edge
    7: 21,                      # operand binding
    8: 9, 9: 8, 0x0a: 4,
    0x20: 8, 0x21: 20, 0x22: 0, 0x23: "str", 0x24: 8, 0x25: 1,
    0x26: "u16list", 0x28: 17, 0x29: 4, 0x2a: "str", 0x2b: 0, 0x2c: 1,
    0x2d: "range", 0x2e: 2, 0x2f: "u32list", 0x30: 0, 0x31: 8, 0x32: 0,
}

CODE_NAMES = {
    1: "path1", 2: "path2", 3: "path3",
    6: "edge", 7: "operand",
    0x23: "expression", 0x2a: "name",
    0x26: "u16list", 0x2f: "u32list", 0x2d: "range",
}

PATH_CODES = (1, 2, 3)

#: Entry names declared in the header of every generation.
ENTRY_NAMES = ("Protokoll", "CommProcessor", "DataProc", "HBProduction",
               "ODXCheck", "SH4Internal", "Version")

OPS = "&|<>*/+-%?='~^"

#: Two-character RPN tokens.  The tokenizer peeks one character ahead, so these
#: must be matched before the single-character forms.
RPN_DIGRAPHS = frozenset(("&&", "||", ">=", ">>", "<=", "<<",
                          "i-", "i/", "r<", "r>", "m+", "M+", "MR"))

_MASK32 = 0xFFFFFFFF


# --------------------------------------------------------------------------
# header
# --------------------------------------------------------------------------

def header(d):
    """The leading length-prefixed ASCII block: version banner and entry list.

    The file opens with a `00 00` pad, so skip leading tag bytes before the
    first length field rather than bailing on it.
    """
    out, i = [], 0
    while i < len(d) and d[i] in (0, 1):
        i += 1
    while i + 2 <= len(d):
        ln = struct.unpack_from("<H", d, i)[0]
        if not (3 <= ln <= 512 and i + 2 + ln <= len(d)):
            break
        s = d[i + 2:i + 2 + ln]
        if not all(32 <= c < 127 for c in s):
            break
        out.append(s.decode("ascii"))
        i += 2 + ln
        while i < len(d) and d[i] in (0, 1):     # tag bytes between entries
            i += 1
    return out, i


def stream_start(d):
    """Offset of the first record: past the ASCII header and the preamble.

    Walks the header by its own framing rather than reusing :func:`header`,
    which tolerates the pad bytes loosely and so cannot land on the preamble
    exactly.  Raises if the `02 00 03 00` preamble is not where it should be --
    a wrong start offset would desynchronise the whole stream silently.
    """
    i = 0
    while i + 4 <= len(d):
        flag, ln = struct.unpack_from("<HH", d, i)
        if flag not in (0, 1) or not (1 <= ln <= 512) or i + 4 + ln > len(d):
            break
        if not all(32 <= c < 127 for c in d[i + 4:i + 4 + ln]):
            break
        i += 4 + ln + (1 if flag == 1 else 0)
    if d[i:i + 4] != b"\x02\x00\x03\x00":
        raise ValueError("DCL preamble not found at %#x" % i)
    return i + 4


# --------------------------------------------------------------------------
# record stream
# --------------------------------------------------------------------------

def body_len(d, code, o):
    """Body length of `code` whose body starts at `o`, or None if unknown."""
    s = CODE_SIZE.get(code)
    if s is None:
        return None
    if s == "str":                                    # u16 n ; byte[n]
        return 2 + struct.unpack_from("<H", d, o)[0]
    if s == "u16list":                                # u16 a,b,n ; u16[n]
        return 6 + 2 * struct.unpack_from("<H", d, o + 4)[0]
    if s == "u32list":                                # u16 a,b,n ; u32[n]
        return 6 + 4 * struct.unpack_from("<H", d, o + 4)[0]
    if s == "range":                                  # u32,i32,u16 n;u16[n];u16
        return 12 + 2 * struct.unpack_from("<H", d, o + 8)[0]
    return s


def records(d, start=None):
    """Yield ``(offset, code, body)`` for every record, in file order.

    Strict by design: raises `ValueError` on the first byte it cannot explain
    rather than resynchronising.  Resync is what produced parsers that looked
    two-thirds successful while dropping most of the file's content, so a hard
    failure here is the feature.  Use :func:`coverage` to locate a stop.
    """
    o = stream_start(d) if start is None else start
    n = len(d)
    while o + 2 <= n:
        code = struct.unpack_from("<H", d, o)[0]
        ln = body_len(d, code, o + 2)
        if ln is None or o + 2 + ln > n:
            raise ValueError("unknown record code %#x at %#x" % (code, o))
        yield o, code, d[o + 2:o + 2 + ln]
        o += 2 + ln


def path_elems(body):
    """``(slot, ident, cls)`` triples of a path record's body."""
    return [(body[i], struct.unpack_from("<H", body, i + 1)[0], body[i + 3])
            for i in range(0, len(body), 4)]


def attributes(d):
    """Yield ``(offset, path, values)`` -- one attribute and everything under it.

    `path` is a list of ``(slot, ident, cls)``; `values` is the list of
    ``(offset, code, body)`` records between this path record and the next.
    One path can own hundreds of values: a whole dataflow graph is a single
    attribute.
    """
    cur = None
    for o, code, body in records(d):
        if code in PATH_CODES:
            if cur is not None:
                yield cur
            cur = (o, path_elems(body), [])
        elif cur is not None:
            cur[2].append((o, code, body))
    if cur is not None:
        yield cur


def edges(d):
    """``(offset, src, srcport, dst, dstport, flags)`` for every dataflow edge."""
    return [(o,) + struct.unpack_from("<5H", b, 0)
            for o, c, b in records(d) if c == 6]


def operands(d):
    """``attr_offset -> [(offset, src, srcport, dst, dstport, seq, const, isconst, opindex), ...]``

    Operand bindings grouped by the attribute that owns them, in file order.

    Grouping is **positional**, not keyed.  An earlier version keyed on
    ``(dst, dport)`` and indexed by ``opindex``, which silently dropped 184 of
    934 records in gen400 to dictionary collision -- and, worse, merged bindings
    from unrelated groups into vectors that never existed.  A conclusion about
    what the 9-placeholder expression computes was drawn from one of those
    fabricated vectors.  Every alternative key was tried and none is lossless
    (``(dst,dport,sport)`` loses 94, ``(dst,dport,sport)``+seq loses 12); the
    stream's own structure is the answer, since a path record opens an attribute
    and the records after it belong to that attribute.  This keeps 934 of 934.
    """
    out = {}
    for o, path, vals in attributes(d):
        rows = []
        for vo, code, b in vals:
            if code != 7:
                continue
            src, sport, dst, dport, seq = struct.unpack_from("<5H", b, 0)
            const = struct.unpack_from("<I", b, 10)[0]
            rows.append((vo, src, sport, dst, dport, seq, const, b[14], b[15]))
        if rows:
            out[o] = rows
    return out


def operand_groups(d, arity=None):
    """``(attr_offset, path, rows)`` for operand groups, optionally by size.

    Matching an expression to its operands is done on arity -- there is no
    pointer from a ``0x23`` record to its operand group.  In gen400 that is
    almost unique for the interesting case: exactly one expression has nine
    placeholders and exactly two groups have nine members.
    """
    out = []
    for o, path, vals in attributes(d):
        rows = [(vo, b) for vo, c, b in vals if c == 7]
        if rows and (arity is None or len(rows) == arity):
            out.append((o, path, rows))
    return out


def operand_vector(d, attr_offset):
    """``[('const', v) | ('dyn', src, srcport), ...]`` for one attribute.

    Takes the owning attribute's offset (see :func:`operand_groups`), because
    the calculate-node numbering the previous signature assumed does not exist
    in the file.
    """
    rows = operands(d).get(attr_offset)
    if not rows:
        return None
    return [("const", r[6]) if r[7] else ("dyn", r[1], r[2]) for r in rows]


def nodes(d):
    """``(cls, ident) -> [(offset, slot, code, body), ...]`` -- objects.

    Keyed on the *first* path element, since a multi-element path addresses a
    sub-object of that object.  Note this is not the calculate-node numbering
    used by :func:`operands`; see the module docstring.
    """
    out = defaultdict(list)
    for o, path, vals in attributes(d):
        slot, ident, cls = path[0]
        for vo, code, body in vals:
            out[(cls & 0x7F, ident)].append((vo, slot, code, body))
    return dict(out)


def _string_value(body):
    n = struct.unpack_from("<H", body, 0)[0]
    return body[2:2 + n]


def expressions(d):
    """``(offset, path, text)`` for every RPN expression in the record stream.

    Offsets are of the string itself, so they compare directly against
    :func:`scan_expressions`.  Empty ``0x23`` payloads are dropped, which is the
    only difference between the two counts.
    """
    out = []
    for o, path, vals in attributes(d):
        for vo, code, body in vals:
            if code != 0x23:
                continue
            s = _string_value(body)
            if s:
                out.append((vo + 4, tuple(path), s.decode("ascii", "replace")))
    return sorted(out)


def name_values(d):
    """``(offset, path, text)`` for every ``0x2a`` string value.

    The six entry names that recur in the body come out of here -- they are
    values, not section headers.  Named to stay clear of the `names` local in
    :func:`sections`, which is a different thing entirely.
    """
    out = []
    for o, path, vals in attributes(d):
        for vo, code, body in vals:
            if code != 0x2a:
                continue
            s = _string_value(body)
            if s:
                out.append((vo + 4, tuple(path), s.decode("ascii", "replace")))
    return sorted(out)


# --------------------------------------------------------------------------
# independent oracle -- must not use the record model
# --------------------------------------------------------------------------

def is_expression(payload):
    """Does `payload` look like an RPN template rather than a name?

    Deliberately conservative: it rejects three real ``0x23`` payloads in every
    generation (``xa'`` twice and ``xA'``, whose only operator is the quote).
    Left as it is because both sides of the acceptance check use it, so the
    comparison stays like-for-like.
    """
    try:
        s = payload.decode("ascii")
    except UnicodeDecodeError:
        return False
    return (bool(s) and any(c in s for c in OPS)
            and all(c in "xXDMR0123456789" + OPS for c in s))


def scan_expressions(d):
    """``(offset, text)`` for every RPN expression, without using the record model.

    Deliberately independent of :func:`records`, because it is the oracle the
    record walk is scored against.  Do not reimplement it on top of the record
    model, however correct that model looks: the whole point is that the two
    agree for unrelated reasons.  They now do -- 43/43 and 49/49, zero missed.
    """
    out, i, n = [], 0, len(d)
    while i + 2 <= n:
        ln = struct.unpack_from("<H", d, i)[0]
        if 3 <= ln <= 512 and i + 2 + ln <= n:
            s = d[i + 2:i + 2 + ln]
            if all(32 <= c < 127 for c in s) and is_expression(s):
                out.append((i + 2, s.decode("ascii")))
                i += 2 + ln
                continue
        i += 1
    return out


def sections(d):
    """(offset, name) for each section marker named in the header entry list.

    The header declares Protokoll / CommProcessor / DataProc / HBProduction /
    ODXCheck / SH4Internal / Version, and those names recur through the body as
    boundaries. Mapping them is the prerequisite for parsing records correctly.
    """
    names = ["Protokoll", "CommProcessor", "DataProc", "HBProduction",
             "ODXCheck", "SH4Internal", "Version"]
    out = []
    for nm in names:
        b, i = nm.encode("ascii"), 0
        while True:
            i = d.find(b, i)
            if i < 0:
                break
            # a real marker is length-prefixed
            if i >= 2 and struct.unpack_from("<H", d, i - 2)[0] == len(b):
                out.append((i - 2, nm))
            i += 1
    return sorted(out)


def section_ranges(d):
    """``(name, start, end)`` byte extents, one per body entry-name occurrence.

    A convenience for saying where something sits, not a framing boundary --
    the names are ordinary string values and the record grammar does not change
    at them.  The span before the first body name is reported as ``header``.
    """
    marks = [(o, nm) for o, nm in sections(d) if o >= 0x200]
    out, prev = [], stream_start(d)
    if marks and marks[0][0] > prev:
        out.append(("header", 0, marks[0][0]))
    elif not marks:
        return [("header", 0, len(d))]
    for i, (o, nm) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(d)
        out.append((nm, o, end))
    return out


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def _walk(d):
    """Tolerant walk for measurement: ``(records, stop_offset, stop_code)``.

    Stops at the first unexplained byte instead of raising, so `coverage` can
    report *where* a future variant diverges rather than just failing.
    """
    o, n, out = stream_start(d), len(d), []
    while o + 2 <= n:
        code = struct.unpack_from("<H", d, o)[0]
        ln = body_len(d, code, o + 2)
        if ln is None or o + 2 + ln > n:
            return out, o, code
        out.append((o, code, 2 + ln))
        o += 2 + ln
    return out, o, None


def coverage(d):
    """Measured bytes explained, per section and overall.

    Returns a dict::

        {'start':      first record offset,
         'parsed':     bytes explained by the record walk,
         'total':      bytes in the record stream,
         'records':    record count,
         'stop':       offset of the first unexplained byte, or None,
         'stop_code':  the code that stopped it, or None,
         'sections':   [(name, start, end, parsed, total), ...]}

    `parsed` counts only bytes the walk actually consumed, so a stop shows up
    as a shortfall rather than being silently skipped.  Per-section bytes are
    clipped to the range, because records straddle these boundaries freely --
    the names are values, not delimiters, so nothing is aligned to them.
    Section figures come out equal because the grammar does not vary by
    section, which is itself the finding.
    """
    recs, stop, stop_code = _walk(d)
    start = stream_start(d)
    parsed = sum(sz for _, _, sz in recs)
    per = []
    for nm, lo, hi in section_ranges(d):
        lo_ = max(lo, start)
        got = sum(min(o + sz, hi) - max(o, lo_)
                  for o, _, sz in recs if o < hi and o + sz > lo_)
        per.append((nm, lo, hi, got, max(0, hi - lo_)))
    return {"start": start, "parsed": parsed, "total": len(d) - start,
            "records": len(recs), "stop": None if stop_code is None else stop,
            "stop_code": stop_code, "sections": per}


def check(d):
    """Acceptance test: coverage plus expression recall against the oracle.

    Returns ``(ok, report_dict)``.  `ok` requires the record walk to explain the
    whole stream *and* to recover exactly the expression set
    :func:`scan_expressions` finds.  Coverage on its own is not sufficient and
    must not be reported as if it were.
    """
    cov = coverage(d)
    oracle = {(o, t) for o, t in scan_expressions(d)}
    walked = {(o, t) for o, _p, t in expressions(d) if is_expression(t.encode())}
    pct = 100.0 * cov["parsed"] / max(1, cov["total"])
    rep = {"coverage_pct": pct, "records": cov["records"],
           "stop": cov["stop"], "stop_code": cov["stop_code"],
           "oracle": len(oracle), "walked": len(walked),
           "missed": sorted(oracle - walked), "extra": sorted(walked - oracle)}
    return (cov["stop"] is None and pct >= 99.999
            and not rep["missed"] and not rep["extra"]), rep


# --------------------------------------------------------------------------
# RPN evaluation
# --------------------------------------------------------------------------

def _sx(v):
    """Truncate to 32 bits, signed -- the interpreter's working width."""
    v &= _MASK32
    return v - 0x100000000 if v & 0x80000000 else v


#: Characters the interpreter's dispatch tree recognises, with the handler each
#: reaches. Recovered by *interpreting* the tree at 0x089062DC per character
#: with delay-slot semantics honoured -- it is compiler-generated binary search
#: that reuses branch delay slots to hold the next comparison, so pairing
#: comparisons with the following branch textually attributes them to the wrong
#: character. Anything absent falls to the reject arm at 0x08906C1A.
RPN_HANDLERS = {
    "x": 0x0890640E, "+": 0x08906442, "*": 0x08906458, "-": 0x08906474,
    "/": 0x0890648A, "&": 0x089064E8, "|": 0x08906550, "%": 0x089065B8,
    "=": 0x089065DE, ">": 0x08906616, "<": 0x089066BC, "?": 0x0890676E,
    "^": 0x08906790, "!": 0x089067B6, "~": 0x089067C8, "i": 0x08906828,
    "o": 0x089068E0, "r": 0x0890692E, "D": 0x089069EA, "d": 0x08906A00,
    "a": 0x08906A16, "A": 0x08906A38, "m": 0x08906B20, "M": 0x08906B54,
}

#: Tokens whose behaviour has been read out of the handler machine code. Every
#: other recognised character is deliberately unimplemented -- see `evaluate`.
RPN_VERIFIED = frozenset("+-*/%^~&|=<>?axDdM")

#: Two-character tokens whose handlers have been read.
RPN_VERIFIED_DIGRAPHS = frozenset(("<<", ">>", "<=", ">=",
                                   "&&", "||", "MR", "M+"))

#: The characters the interpreter recognises at all, recovered by emulating its
#: dispatch -- a binary search over the character value, not a compare chain --
#: for every printable byte. The result reproduces all 24 handler addresses in
#: RPN_HANDLERS and finds no twenty-fifth, so the operator set is complete.
#: Everything outside this set is skipped by the interpreter; see `evaluate`.
RPN_RECOGNISED = frozenset(RPN_HANDLERS)


def _trunc_div(a, b):
    """Integer division truncating toward zero, as C and __sdivsi3_i4 do.

    Python's // floors, which differs from C whenever exactly one operand is
    negative: -7 // 2 is -4 where C gives -3. The remainder built on top of it
    inherits the discrepancy, so both operators need this rather than //.
    """
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def evaluate(expr, ops, memory=0, strict=True):
    """Evaluate an RPN template, using only semantics verified from the firmware.

    The template interpreter in `PCM3Reload` is a **stack machine**, not a
    compiler: each character's handler does its arithmetic inline, with `@r9`
    the top of stack, `@(4,r9)` the next entry and `r12` the depth. It is a
    separate mechanism from `CRBDiagCalculate::calculate` and its 22 opcodes,
    which serve the dataflow-graph nodes. Do not mix the two.

    **Which slot is the top matters, and is settled by the push handler**, not
    by reading the binary operators. `x` stores the operand at ``r9+8`` and then
    advances ``r9`` by 4, leaving it at ``@(4,r9)``; the shared epilogue writes
    the result to ``@r9`` and then drops ``r9`` by 4, putting it at the new
    ``@(4,r9)``. So::

        @(4,r9)   top of stack
        @r9       the element below it

    Subtraction is therefore **conventional**: the `-` handler loads ``@r9``
    into r1 and ``@(4,r9)`` into r2 and computes ``sub r2,r1``, i.e.
    ``second - top``, so `a b -` yields `a - b`.

    An earlier revision of this function asserted the opposite, having inferred
    the stack direction from the binary operators instead of the push. The
    operands are symmetric there and the mistake is invisible in `+` and `*`;
    it only shows up in `-`, where it silently negates every result.

    Implemented, each read from its handler:

    ==========  ==============  ==========================================
    token       handler         operation
    ==========  ==============  ==========================================
    ``x``       ``0890640E``    push the next operand
    ``+``       ``08906442``    ``add r2,r1``     second + top
    ``-``       ``08906474``    ``sub r2,r1``     second - top
    ``*``       ``08906458``    ``mul.l``         second * top
    ``^``       ``08906790``    ``xor r2,r1``
    ``&``       ``089064E8``    bitwise and
    ``|``       ``08906550``    bitwise or
    ``~``       ``089067C8``    ``not`` on ``@(4,r9)``, in place -- unary
    ``=``       ``089065DE``    ``cmp/eq`` then ``movt`` -- equality, 0 or 1
    ==========  ==============  ==========================================

    ``/`` and ``%`` both call the imported ``__sdivsi3_i4`` through the PLT stub
    at ``0804B404`` -- SH4 signed division, ``r4`` dividend, ``r5`` divisor,
    quotient returned in **FPUL** (that is what the ``_i4`` suffix means, and
    why capstone shows the following ``sts fpul,r7`` as undecoded bytes)::

        /   r4 = @r9 (second), r5 = @(4,r9) (top)
            sts fpul,r7 ; mov.l r7,@r9                 second / top

        %   same call, then
            mul.l r8,r7 ; sts macl,r1 ; sub r1,r6      second - (second/top)*top

    So both are conventional, and ``%`` is C's remainder over a **truncating**
    division -- the sign follows the dividend, not Python's floor semantics.

    The ``tst r4,r4`` early in ``/`` is a zero-*numerator* shortcut, not a
    divisor guard: it branches to the epilogue variant that skips the store,
    leaving ``second`` (which is 0) as the result. Division by zero is not
    guarded here, so this raises rather than inventing a value.

    The logical pair is **not** C's truthiness. ``&&`` and ``||`` test each
    operand with ``cmp/pl`` -- *greater than zero* -- so a negative operand is
    false where C would call it true. ``?`` is different again: it tests its
    condition with ``tst``, i.e. non-zero, and takes three operands, storing the
    result to ``@r10`` (the third slot), so ``c a b ?`` is ``c ? a : b``.

    **Characters with no handler are no-ops, not errors.** The dispatch is a
    binary search over the character value; emulating it across every printable
    byte accepts exactly the 24 characters in :data:`RPN_HANDLERS` and finds no
    twenty-fifth, so the operator set is complete. Everything else branches to
    ``08906C1A`` -- which is the shared loop tail that ordinary operators return
    through as well, not an error arm. It increments the character index and
    branches back to the loop head, leaving the stack untouched.

    That resolves ``'``, which appears in shipped expressions such as
    ``xx*xx*xx*|'|`` yet has no handler: the record length fields prove it is
    genuine payload rather than a slicing artifact, and it is simply skipped.
    The sole exception is an empty stack, where the arm is entered with T set
    from a ``tst r12,r12`` delay slot and exits through the error report.

    ``a`` and ``A`` are named by the interpreter's own trace strings --
    ``'absolut %d; '`` at ``0952ACB4`` and ``'Average; '`` at ``0952ACC4``.

    ``a`` is absolute value, unary in place, and is implemented.

    ``A`` is **read but deliberately not implemented.** It averages only when it
    is the last character of the expression, or the second-to-last followed by
    ``'`` (``08906A6A`` / ``08906A72`` / ``08906A7C``) -- exactly the shape
    ``xA'``. It then drains the stack into a sum at ``r14+124`` with a count in
    r13 and divides through ``__sdivsi3_i4``. But the ``depth--`` at ``08906A82``
    happens *before* the loop guard, so on a one-element stack the loop never
    runs and the count stays zero: a bare ``xA'`` would divide by zero. The
    operator only yields a value once the dataflow node array at ``ctx+84``
    contributes, which is the same reason the nine-placeholder expressions
    cannot be evaluated standalone. Refusing is the correct outcome here, not a
    gap in the reading.

    Both also consume more than one character: ``a`` advances the index by two
    at ``08906A2E`` on top of the tail's one. In the shipped data its only
    occurrence is ``xa'``, where what it swallows is the no-op ``'`` and the end
    of the string, so the value is unaffected either way.

    The rounding and inversion tokens (``!``, ``i``, ``o``, ``r``, ``m``) have
    handlers whose stack effects were not read, and still raise.
    A previous version guessed at all of these and claimed
    confirmation against "node 187", which turned out to be an artifact of a
    keying bug in :func:`operands` -- the expression at that ident is not what
    was assumed. Refusing is better than returning a number nobody can trust.

    Pass ``strict=False`` to skip those recognised-but-unread tokens instead of
    raising. That is not the same as the no-op path above: it will silently
    change the result, and is only useful for surveying evaluability.

    Digraphs (``&&``, ``||``, ``>=``, ``>>``, ``<<``, ``<=``, ``MR`` ...) are
    handled by the *first* character's handler peeking the next one, confirmed
    by `cmp/eq` against the following character inside each handler.
    """
    st, i, k, n = [], 0, 0, len(expr)
    denial = False
    memory_pushed = False
    while i < n:
        tok = expr[i:i + 2]
        if tok in RPN_DIGRAPHS:
            i += 2
        else:
            tok = expr[i]
            i += 1

        if tok == "x":
            if k >= len(ops):
                raise ValueError("expression %r wants operand %d of %d"
                                 % (expr, k, len(ops)))
            st.append(_sx(ops[k]))
            k += 1
            continue
        if tok in ("+", "-", "*", "^", "&", "|", "="):
            if len(st) < 2:
                raise ValueError("stack underflow at %r in %r" % (tok, expr))
            top = st.pop()
            second = st.pop()
            if tok == "+":
                st.append(_sx(second + top))
            elif tok == "-":
                st.append(_sx(second - top))    # second - top; see docstring
            elif tok == "*":
                st.append(_sx(second * top))
            elif tok == "^":
                st.append(_sx(second ^ top))
            elif tok == "&":
                st.append(_sx(second & top))
            elif tok == "|":
                st.append(_sx(second | top))
            else:
                st.append(int(second == top))
            continue
        if tok in ("D", "d"):
            # Side-effect only. Both write a flag at ctx+44 and ctx+56 and
            # touch neither the stack pointers nor the depth, so they are
            # transparent to the value -- `xDx=` is just `a == b` with a flag
            # raised. Recorded for the caller, not returned in the result.
            denial = (tok == "D")
            continue
        if tok == "MR":
            memory_pushed = True
            st.append(_sx(memory))          # read ctx+16, push like `x`
            continue
        if tok in ("M", "M+"):
            if not st:
                raise ValueError("stack underflow at %r in %r" % (tok, expr))
            # Neither adjusts r9: the top is read, not popped.
            memory = _sx(st[-1] if tok == "M" else memory + st[-1])
            continue
        if tok in ("<", ">", "<=", ">="):
            if len(st) < 2:
                raise ValueError("stack underflow at %r in %r" % (tok, expr))
            top = st.pop()
            second = st.pop()
            # SH cmp/gt and cmp/ge are signed, and _sx has already made these
            # signed, so plain Python comparison matches.
            st.append(int({"<": second < top, ">": second > top,
                           "<=": second <= top, ">=": second >= top}[tok]))
            continue
        if tok in ("<<", ">>"):
            if len(st) < 2:
                raise ValueError("stack underflow at %r in %r" % (tok, expr))
            top = st.pop()
            second = st.pop()
            # Not `n`: that is the loop bound. Assigning it here ended the walk
            # early on every expression containing a shift, silently returning
            # a partial result rather than raising.
            sh = top & 31           # SH shld/shad take the low 5 bits
            if tok == "<<":
                st.append(_sx(second << sh))
            else:
                # `neg` then `shad` -- arithmetic, so the sign propagates.
                st.append(_sx(second >> sh))
            continue
        if tok in ("/", "%"):
            if len(st) < 2:
                raise ValueError("stack underflow at %r in %r" % (tok, expr))
            top = st.pop()
            second = st.pop()
            if top == 0:
                raise ZeroDivisionError(
                    "%r in %r divides by zero; the interpreter does not guard "
                    "this and its behaviour was not established" % (tok, expr))
            q = _trunc_div(second, top)
            st.append(_sx(q if tok == "/" else second - q * top))
            continue
        if tok == "~":
            if not st:
                raise ValueError("stack underflow at '~' in %r" % expr)
            st.append(_sx(~st.pop()))
            continue
        if tok == "a":
            if not st:
                raise ValueError("stack underflow at 'a' in %r" % expr)
            # Absolute value, unary in place: 08906A26 loads @(4,r9) as the
            # argument and 08906A28 stores the result straight back to it, so
            # the depth is untouched. Named by its own trace string,
            # 'absolut %d; ' at 0952ACB4.
            st.append(_sx(abs(st.pop())))
            continue
        if tok in ("&&", "||"):
            if len(st) < 2:
                raise ValueError("stack underflow at %r in %r" % (tok, expr))
            top = st.pop()
            second = st.pop()
            # `cmp/pl`, which is "greater than zero", NOT "non-zero". A negative
            # operand is false here where C would call it true. Read from
            # 0890650C/08906572; both then join the shared binary epilogue, so
            # each pops two and pushes one.
            a, b = second > 0, top > 0
            st.append(int(a and b if tok == "&&" else a or b))
            continue
        if tok == "?":
            if len(st) < 3:
                raise ValueError("stack underflow at '?' in %r" % expr)
            top = st.pop()
            second = st.pop()
            cond = st.pop()
            # 08906782: `tst r1,r1` on the third slot -- this one really is
            # non-zero, unlike && and ||. The result is stored to @r10, the
            # third slot, so `c a b ?` pops three and pushes one.
            st.append(second if cond != 0 else top)
            continue

        if tok in RPN_HANDLERS or tok in RPN_DIGRAPHS:
            if strict:
                raise NotImplementedError(
                    "token %r in %r is recognised by the interpreter "
                    "(handler %s); its stack effect has not been read from the "
                    "firmware, so evaluating would be a guess"
                    % (tok, expr,
                       "0x%08X" % RPN_HANDLERS[tok] if tok in RPN_HANDLERS
                       else "digraph"))
            continue

        # Not recognised by the interpreter at all. Its dispatch is a binary
        # search over the character value; emulating that tree for every
        # printable byte accepts exactly the 24 characters in RPN_HANDLERS and
        # sends the rest to the arm at 08906C1A -- which is the *shared loop
        # tail* that ordinary operators also return through, not an error path.
        # It advances the character index and loops (`bra 0x89062c4`), so the
        # character is skipped with the stack untouched.
        #
        # This is why "'" appears in shipped expressions such as `xx*xx*xx*|'|`
        # while having no handler: it is a no-op. The record length fields
        # confirm it is genuine payload and not a slicing artifact.
        #
        # The one exception is an empty stack: the arm is entered with T set
        # from a `tst r12,r12` delay slot, and depth zero exits through the
        # error report instead of looping.
        if not st:
            raise ValueError(
                "unrecognised token %r in %r reached with an empty stack; the "
                "interpreter exits through its error report here" % (tok, expr))
    if not st:
        raise ValueError("expression %r produced no value" % expr)
    return st[-1]


def evaluable(expr):
    """True when every token in `expr` has verified semantics.

    Use to select which expressions :func:`evaluate` can handle before trying,
    rather than catching NotImplementedError per expression.
    """
    i, n = 0, len(expr)
    while i < n:
        tok = expr[i:i + 2]
        if tok in RPN_DIGRAPHS:
            if tok not in RPN_VERIFIED_DIGRAPHS:
                return False
            i += 2
            continue
        tok = expr[i]
        i += 1
        # A character the interpreter does not recognise is a no-op, not an
        # obstacle: it is skipped by the shared loop tail. Only a character it
        # *does* dispatch, but whose handler has not been read, blocks us.
        if tok in RPN_RECOGNISED and tok not in RPN_VERIFIED:
            return False
    return True


# The previous version carried a _BINARY table covering every token -- division,
# modulo, all six comparisons, the logical pair and the "invers" forms -- with
# operand orders that were assumed rather than read. It is deleted rather than
# kept for reference: a plausible table sitting next to a verified one is how
# the wrong semantics get used by accident. The verified operations live inline
# in evaluate(), each traceable to a handler address.


def bind(vector, value):
    """Operand list for :func:`evaluate`: constants kept, dynamic slots = `value`."""
    return [value if slot[0] == "dyn" else slot[1] for slot in vector]


def summary(d):
    """Human-readable overview, with the acceptance check first."""
    hdr, _hend = header(d)
    ok, rep = check(d)
    cov = coverage(d)
    lines = ["DCL diagnostic description, %d bytes" % len(d)]
    for h in hdr[:3]:
        lines.append("   %s" % h)
    lines.append("")
    lines.append("   stream starts at %#x" % cov["start"])
    lines.append("   %d records, %d/%d bytes explained (%.2f%%)%s"
                 % (cov["records"], cov["parsed"], cov["total"],
                    rep["coverage_pct"],
                    "" if cov["stop"] is None
                    else "  STOPPED at %#x on code %#x"
                         % (cov["stop"], cov["stop_code"])))
    lines.append("   expressions: %d by record walk, %d by string scan%s"
                 % (rep["walked"], rep["oracle"],
                    "" if not rep["missed"]
                    else "  MISSED %d" % len(rep["missed"])))
    lines.append("   acceptance: %s" % ("PASS" if ok else "FAIL"))
    lines.append("")
    for nm, lo, hi, got, span in cov["sections"]:
        lines.append("   %-14s %#08x-%#08x  %6d/%6d bytes"
                     % (nm, lo, hi, got, span))
    types = defaultdict(int)
    for o, code, _b in records(d):
        types[code] += 1
    top = sorted(types.items(), key=lambda kv: -kv[1])[:10]
    lines.append("")
    lines.append("   codes: %s"
                 % " ".join("%s=%d" % (CODE_NAMES.get(c, "%02x" % c), n)
                            for c, n in top))
    return "\n".join(lines)
