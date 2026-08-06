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

**Binding is by node number, not by arity** -- see :func:`expression_operands`.
Every attribute is a node of a dataflow graph, and node numbers are local to the
graph that references them.  For a graph at attribute index ``gi`` with largest
referenced node ``nmax``, ``S = gi - nmax - 1`` maps node to attribute.  An
expression's operands are then the code-6 and code-7 records aimed at its node
on port 0, replayed in file order through the firmware's registrar.

That is why arity matching never worked: an expression and its operands are
always in *different* attributes, so there was nothing within one attribute to
match.  :func:`operands` and :func:`operand_groups` predate this and group at
the attribute level, which unions unrelated lists; the real unit is the
``(graph, dst, dstport)`` triple.

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

Expressions now bind to their operands and can be computed.  All 52 bind, 49
with one slot per placeholder, and the nine-placeholder expression resolves to
a byte-order conversion: one dynamic source repeated three times against masks
``0xFF0000/0xFF00/0xFF`` and shifts ``16/8/8``, giving ``0x123456 -> 0x345612``.
That it lands on a recognisable idiom, rather than plausible-looking numbers, is
the strongest evidence the binding is right -- a misaligned map yields garbage.

The node map is **derived**, not fitted.  Code-4 and code-9 records frame the
stream into seven config blocks -- the same seven named in the header -- and a
block boundary recreates the node vector, which is the reset that makes
numbering restart.  ``S`` is constant within every span, distinct across all
seven, and equals that span's marker attribute index in 7 of 7 cases.  The
earlier per-graph form ``S = gi - nmax - 1`` is numerically identical (0
disagreements over 237 graphs) but was a free parameter per graph, so it had to
be reported as fitted.  See :func:`blocks` and :func:`node_map`.

What remains unsupported is small and named: the ``n == base`` special case is
a tie-break the firmware does not justify, and the map without it scores
identically on the arity-free signature test.

Still not established: whether the KWP SecurityAccess seed/key derivation lives
in this file.  No evidence either way.

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
    1: 4, 2: 8, 3: 12,          # trailing path list, 1/2/3 elements
    4: 0, 5: 0,                 # eCfgEnd, eFileEnd -- bare block tags
    6: 10,                      # dataflow edge
    7: 21,                      # operand binding
    8: 9, 9: 4, 0x0a: 4,
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

def body_len(d, code, o, prev=None):
    """Body length of `code` whose body starts at `o`, or None if unknown.

    `prev` is the preceding record's code, needed for one genuine ambiguity:
    **code 3 is both a three-element trailing path list and the ``eCfg`` block
    tag.** They are told apart by context -- a block tag only ever follows the
    ``eCfgEnd`` tag (code 4), and a trailing list only ever follows the record
    it belongs to. Nothing in the two bytes themselves distinguishes them.
    """
    if code == 3 and prev == 4:
        return 0                                      # eCfg, a bare block tag
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
    prev = None
    while o + 2 <= n:
        code = struct.unpack_from("<H", d, o)[0]
        ln = body_len(d, code, o + 2, prev)
        if ln is None or o + 2 + ln > n:
            raise ValueError("unknown record code %#x at %#x" % (code, o))
        yield o, code, d[o + 2:o + 2 + ln]
        o += 2 + ln
        prev = code


def path_elems(body):
    """``(slot, ident, cls)`` triples of a path record's body."""
    return [(body[i], struct.unpack_from("<H", body, i + 1)[0], body[i + 3])
            for i in range(0, len(body), 4)]


def attributes(d):
    """Yield ``(offset, path, values)`` -- one attribute and everything under it.

    `path` is a list of ``(slot, ident, cls)``; `values` is the list of
    ``(offset, code, body)`` records the path belongs to. One path can own
    hundreds of values: a whole dataflow graph is a single attribute.

    **The path is a trailing field of the records BEFORE it, not a header for
    the records after it.** Every configurator case for code >= 0x20 ends by
    reading ``u16 n ; u32[n]``, while codes 6/7/8/9/0x0a do not, so what looks
    like a standalone "path record" of code n is that trailing list. The bytes
    are identical either way, which is why walking it as a separate record still
    reached 100% coverage and the error went unnoticed -- but every path was
    attributed to the following record instead of its own.

    Confirmed on the shipped file in both directions with zero exceptions:
    2900 of 2900 records with code >= 0x20 are immediately followed by a path
    record, 2900 of 2900 path records are immediately preceded by one, and the
    body length is exactly ``4 * code``.

    This relabels groups rather than re-partitioning them, so membership is
    unchanged and every index shifts by one -- which :func:`node_map` absorbs
    into ``S``, leaving the operand binding intact.
    """
    pend = []
    for o, code, body in records(d):
        # A code-3 with an empty body is the eCfg block tag, not a path list.
        # Treating it as a path inserts six spurious attribute boundaries and
        # shifts every index after them.
        if code in PATH_CODES and body:
            # The path CLOSES the run before it; it does not open the run after.
            yield (pend[0][0] if pend else o, path_elems(body), pend)
            pend = []
        else:
            pend.append((o, code, body))
    if pend:
        # Records after the final path list. None exist in any shipped file,
        # but a truncated one would land here rather than vanishing.
        yield (pend[0][0], [], pend)


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


def blocks(d):
    """Attribute indices at which each config block begins.

    The stream is partitioned into blocks by code-4 and code-9 records -- the
    block framing the firmware's configurator reads. There are exactly seven in
    the shipped file, matching the seven entry names in the header.

    The marker sits in the **last** attribute of the block it closes, and that
    same attribute index is where the next block's node numbering starts from.
    Both facts are needed: membership tests are strict (a graph belongs to the
    block whose marker index is strictly below it), while the returned index is
    the origin the next block counts from.
    """
    out = [0]
    for i, (o, path, vals) in enumerate(attributes(d)):
        if any(c in (4, 9) for _, c, _ in vals):
            out.append(i)
    return sorted(set(out))


def graphs(d):
    """``attr_index -> {rows, base, nmax, S}`` for every dataflow graph.

    A graph attribute is one holding code-6 or code-7 records. Node numbers are
    **local to the block**, and the whole binding problem turns on converting
    them to attribute indices; see :func:`node_map`.

    ``S`` is the origin: attribute index of the marker of the block this graph
    belongs to. It is **derived**, not fitted. The equivalent per-graph formula
    ``S = gi - nmax - 1`` reproduces it exactly on all 237 graphs, but that form
    invites recovering a free parameter per graph; the block origin explains
    *why* the values collapse to seven, and why they collapse to exactly the
    number of config blocks.
    """
    bl = blocks(d)
    out = {}
    for gi, (o, path, vals) in enumerate(attributes(d)):
        rows = []
        for vo, c, b in vals:
            if c not in (6, 7):
                continue
            src, sport, dst, dport, seq = struct.unpack_from("<5H", b, 0)
            if c == 6:
                rows.append((6, vo, src, sport, dst, dport, seq, 0, 0, 0, 0))
            else:
                rows.append((7, vo, src, sport, dst, dport, seq,
                             struct.unpack_from("<I", b, 10)[0],  # const
                             b[14],                               # isconst
                             struct.unpack_from("<I", b, 15)[0],  # opindex
                             b[19]))                              # hasindex
        if not rows:
            continue
        ns = {r[2] for r in rows} | {r[4] for r in rows}
        # Strict: the marker's own attribute closes its block, so a graph
        # sitting on a boundary belongs to the block before it, not after.
        origin = max((x for x in bl if x < gi), default=0)
        out[gi] = {"rows": rows, "offset": o, "base": min(ns), "nmax": max(ns),
                   "S": origin}
    return out


def node_map(d):
    """``(attr2node, node2attr)`` -- node numbers <-> attribute indices.

    ``attr(n) = n + S``, where ``S`` is the origin of the block the graph
    belongs to -- see :func:`blocks`.

    **The map is derived, not fitted.** The firmware says node numbers are
    dense indices into a vector; a block boundary recreates that vector, which
    is the reset that makes numbering restart. Concretely, the seven code-4/9
    markers cut the file into seven spans, ``S`` is constant within every span
    and distinct across all seven, and it equals the attribute index of that
    span's marker in 7 of 7 cases.

    An earlier revision recovered ``S = gi - nmax - 1`` per graph instead. That
    is numerically identical -- 0 disagreements over all 237 graphs, and the two
    maps are equal entry for entry -- but it is a free parameter per graph that
    happens to collapse to seven values, which is why it had to be reported as
    fitted. The block origin explains *why* there are seven.

    Two traps, both of which pass every endpoint check while breaking interior
    binding. Dropping the ``-1`` from the old formula still satisfies
    ``base == min(node)`` on 237/237 and still yields exactly 7 values. And
    block membership must be **strict**: the marker's attribute closes its own
    block, so testing ``<=`` instead of ``<`` misassigns every boundary graph.

    An ``n == base`` special case used to sit here as an unexplained tie-break.
    It is gone: with it and without it the map tiles 2900 attributes with zero
    duplicates, scores 5 signatures at 100.0% purity with nothing unresolved,
    and binds 52 expressions with 49 exact and the same three residuals. It
    changed nothing, so it was carrying no information.
    """
    attr2node, node2attr = {}, {}
    for gi, g in graphs(d).items():
        for n in range(g["base"], g["nmax"] + 1):
            a = n + g["S"]
            node2attr[(gi, n)] = a
            attr2node[a] = (gi, n)
    return attr2node, node2attr


def operand_slots(d, gi, nd, _graphs=None):
    """The operand array for node `nd` of graph `gi`, indexed by slot.

    Replays the firmware's input registrar. Code-6 **and** code-7 records reach
    it through the same path (``089053D8`` -> ``08910C90`` -> ``089113B0`` ->
    ``vtbl[7]`` = ``08906FB8``), which is why considering only code 7 leaves an
    expression unbound: two code-6 edges register its operands instead.

        dport not in (0, 1)  discarded by the registrar outright
        dport == 1           event input, never an operand
        dport == 0           slot = opindex when a code-7 row carries an index
                             object (body byte 19), else the next free slot;
                             grow the flag vector to slot+1; count = seq+1

    The operand count is ``len(...)`` -- ``max(slot)+1`` -- **not** the row
    count and not the number of distinct opindex values. Rows sharing a slot are
    last-write-wins alternatives (``Collect`` at ``08906CBC`` destroys the
    previous occupant), so they are one placeholder, not several.

    Returns a list indexed **by slot**; unwritten slots are ``None``. Do not
    compact it: ranking the slots densely agrees almost everywhere and silently
    shifts the one expression whose slot 0 is never written.
    """
    g = (_graphs or graphs(d))[gi]
    flags, count = [], 0
    for r in sorted(g["rows"], key=lambda r: r[1]):        # file order
        code, vo, src, sport, dst, dport, seq, const, isconst, opix, hasidx = r
        if dst != nd or dport != 0:
            continue
        if seq < count:                                    # cmp/ge at 08906FD8
            continue
        slot = opix if (code == 7 and hasidx) else len(flags)
        while len(flags) <= slot:
            flags.append(None)
        flags[slot] = (("const", const) if (code == 7 and isconst)
                       else ("dyn", src, sport))
        count = seq + 1
    return flags


def kwp_services(d):
    """``[{key, keylen, entry, attr, exprs}, ...]`` -- the code-8 service table.

    Code 8 is the bridge from a diagnostic request to the dataflow graph. The
    firmware's mapper at ``08904264`` fails with ``"cannot map service id 0x%X
    (size %d byte(s))"``, which names the first two fields::

        u32 key ; u8 keylen ; u16 entry_node ; u16 extra          (9 bytes)

    ``keylen`` is the request's length in bytes, and ``key`` is the request
    itself: a ``keylen`` of 2 with key ``0x2106`` is the request ``21 06``.
    Observed prefixes are the KWP services you would expect -- ``0x21``
    readDataByLocalIdentifier, ``0x31`` routineControl, ``0x33``
    requestRoutineResults, ``0x3B`` writeDataByLocalIdentifier, ``0x1A``
    readEcuIdentification.

    **Checked against a live PCM 3.1 on the bench.** The two-byte form is
    confirmed: ``21 06``, ``21 0A``, ``21 0C``, ``21 11`` and ``21 F4`` are all
    served and answer with data.

    The one-byte form is **not** a local identifier, which the unit settled by
    rejecting ``21 3C``, ``21 35`` and ``21 36`` with subFunctionNotSupported --
    all three are keylen-1 keys, and reading them as ``21 <key>`` was my guess,
    not something the file said. Only 14 of the 37 one-byte keys are standard
    KWP service ids either, so what that form addresses is open.

    Practical note for anyone querying these: the first request after a session
    opens frequently comes back ``7F xx 21`` (busyRepeatRequest) or silent. It
    is transient -- repeat and it answers.

    ``entry`` is a node number in the owning graph's block-local numbering, so
    it resolves through :func:`node_map` like any other. ``exprs`` lists the
    expression attributes reachable from it along code-6 edges.

    This is what makes the model testable against a unit: a request whose
    expression has known constants predicts something about the answer. Treat a
    match as evidence and a mismatch as inconclusive -- reachability shows the
    expression is downstream of the request, not that its value is what the
    response carries.

    That caveat turned out to be the whole story on the bench. ``21 06`` answers
    ``80 01 06 00 00 15`` and ``21 0A`` answers twenty-one bytes: these are
    multi-field records, so a prediction over *every* byte tests nothing. What
    is still missing is the output layout -- which node's value lands in which
    byte of the response. Until that is decoded, an expression can be shown to
    sit downstream of a request but not tied to a field of its answer.
    """
    attrs = list(attributes(d))
    gs = graphs(d)

    expr_at = {}
    for j, (o, path, vals) in enumerate(attrs):
        for vo, c, b in vals:
            if c == 0x23:
                t = _string_value(b).decode("latin1")
                if t:
                    expr_at[j] = t

    fwd = defaultdict(lambda: defaultdict(set))
    for gi, g in gs.items():
        for r in g["rows"]:
            if r[0] == 6:
                fwd[gi][r[2]].add(r[4])

    out = []
    for gi, (o, path, vals) in enumerate(attrs):
        for vo, c, b in vals:
            if c != 8 or len(b) < 9:
                continue
            key, klen = struct.unpack_from("<IB", b, 0)
            entry = struct.unpack_from("<H", b, 5)[0]
            S = gs[gi]["S"] if gi in gs else 0
            seen, stack = {entry}, [entry]
            while stack:
                for nxt in fwd[gi].get(stack.pop(), ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            out.append({"key": key, "keylen": klen, "entry": entry,
                        "attr": entry + S, "graph": gi,
                        "exprs": sorted((n + S, expr_at[n + S]) for n in seen
                                        if n + S in expr_at)})
    return out


#: Scalar field widths, read from the jump table the firmware's
#: ``CDiagParam::getByteSize`` (``08900734``) branches through: 16 entries at
#: ``0890074C`` with braf base ``0890074A``. Types 9-14 take the length field at
#: ``[this+8]`` instead, i.e. they are variable and the config does not carry
#: their size.
DIAG_WIDTH = {0: 0, 1: 0, 2: 1, 3: 1, 4: 2, 5: 2, 8: 3, 6: 4, 7: 4, 15: 4}

#: Buffer types whose length the IO subsystem supplies at run time.
DIAG_VARIABLE = frozenset((9, 10, 11, 12, 13, 14, 18))


def _owning_record(vals):
    """The value record of an attribute: the last one, codes >= 0x20.

    A trailing path list belongs to the record before it, so an attribute is a
    run of graph records followed by the one value record that owns the path.
    """
    for vo, c, b in reversed(vals):
        if c >= 0x20:
            return c, b
    return None, None


def node_width(d, gi, n, _ctx=None, _depth=0):
    """Byte width of a node's contribution to a response, or None if runtime.

    The firmware never stores a response length: ``CDiagParams::getByteSize``
    (``08901348``) is a pure sum over the parameter list, and each parameter's
    width comes from its type through ``CDiagParam::getByteSize``
    (``08900734``). So the length is reconstructed here the same way -- by
    summing widths -- rather than looked up.

    Returns ``None`` for a variable-width buffer, which is an abstention rather
    than a failure: for a type-18 signal the length genuinely is not in the
    file, and inventing a constant for it is how a fitted parameter gets in.
    """
    if _ctx is None:
        attrs = list(attributes(d))
        _ctx = (attrs, graphs(d), node_map(d)[0])
    attrs, gs, a2n = _ctx
    if _depth > 12 or gi not in gs:
        return None
    a = n + gs[gi]["S"]
    if not (0 <= a < len(attrs)):
        return None
    code, body = _owning_record(attrs[a][2])

    if code == 0x28 and len(body) >= 16:                 # CRBDiagIOSub
        t = struct.unpack_from("<i", body, 12)[0]
        if t in DIAG_VARIABLE:
            return None
        return DIAG_WIDTH.get(t, 1)
    if code == 0x26 and len(body) >= 8:                  # CRBDiagFormat
        return max(1, struct.unpack_from("<H", body, 6)[0] // 8)
    if code in (0x24, 0x31, 0x23, 0x2c, 0x25):           # Const / RPN / char
        return 1
    if code == 0x2f:                                     # CRBDiagTable
        return _table_length(body)
    if code in (0x22, 0x2b):                             # Collector / Pass
        # A Pass node often carries a declared format table on port 1. That is
        # the second mechanism, and it covers what summing widths cannot: the
        # fixed-width text buffers, where the structural walk abstains.
        for r in gs[gi]["rows"]:
            if r[4] == n and r[5] == 1:
                w = node_width(d, gi, r[2], _ctx, _depth + 1)
                if w:
                    return w
        tot = 0
        for r in gs[gi]["rows"]:
            if r[4] != n or r[5] != 0:
                continue
            w = (1 if (r[0] == 7 and r[8])               # a const occupies 1
                 else node_width(d, gi, r[2], _ctx, _depth + 1))
            if w is None:
                return None
            tot += w
        return tot or None
    return None


def _table_length(body):
    """Response length from a ``0x2f`` format table, or None.

    ``0x2f`` is CRBDiagTable, not a plain u32 list: ``u16 kind ; u16 ncols ;
    u16 n ; u32[n]`` with ``n = nrows * ncols``. A ``kind`` of 1 with two
    columns is the response format, and the sum of column 1 over its rows is
    the byte length.
    """
    if len(body) < 6:
        return None
    kind, ncols, n = struct.unpack_from("<3H", body, 0)
    if kind != 1 or ncols != 2 or n < 2 or len(body) < 6 + 4 * n:
        return None
    vals = struct.unpack_from("<%dI" % n, body, 6)
    return sum(vals[1::2]) or None


#: Lengths measured on a live PCM 3.1 that the config provably cannot supply.
#: These are **measurements, not derivations**, and are kept separate from
#: :func:`response_length` for that reason -- folding a measured constant into
#: the derived rule is how a fitted parameter gets back in, and two agents
#: proposed exactly that to claim 17/17 on this one identifier.
#:
#: ``21 1B`` is a type-18 (VECTOR8) buffer on signal 20193. Searched and found
#: absent: the config never slices that signal, so no width is implied there;
#: ``PCM3Reload`` holds no signal-size table (its 65 raw hits for 20193 show no
#: consistent nearby size, and the id search is noise-dominated at that scale);
#: and neither ifs1 nor ifs2 carries an IO-subsystem declaration file. The
#: length is supplied by the IO subsystem at run time.
#:
#: Worth noting: this one's value was byte-identical across two bench runs
#: (``20 32 1a 25 5a 69``) where ``21 12`` and ``21 FE`` both changed, so it is
#: a static identifier rather than live data.
MEASURED_LENGTH = {0x211B: 6}


def response_length(d, key, keylen=2, measured=False):
    """Predicted byte length of the answer to a KWP request, or None.

    Pass ``measured=True`` to fall back to :data:`MEASURED_LENGTH` for requests
    whose length the file cannot carry. The default is derivation only, so a
    caller cannot mistake a bench reading for something the config said.

    The response producers are the sources of every code-6/code-7 row aimed at
    the service's entry node on **port 1** -- a destination port is a function
    index, and function 1 on ``CRBDiagService`` is Response. Multiple producers
    are mutually exclusive outcome paths rather than concatenated fields, so
    they must agree; where they disagree this returns the one they agree on
    most, and callers should treat a split as a warning.

    Validated against a live PCM 3.1. Out of sample it predicted ``21 00`` = 1
    and ``21 16`` = 11 while both were unserved, and a later bench run measured
    exactly those.
    """
    attrs = list(attributes(d))
    gs = graphs(d)
    ctx = (attrs, gs, node_map(d)[0])
    for s in kwp_services(d):
        if s["key"] != key or s["keylen"] != keylen:
            continue
        gi, E = s["graph"], s["entry"]
        if gi not in gs:
            continue
        widths = []
        for r in gs[gi]["rows"]:
            if r[4] == E and r[5] == 1:
                w = node_width(d, gi, r[2], ctx)
                if w:
                    widths.append(w)
        if widths:
            return max(set(widths), key=widths.count)
    return MEASURED_LENGTH.get(key) if measured else None


def expression_operands(d):
    """``[(attr_index, offset, text, slots), ...]`` -- every expression, bound.

    This supersedes matching on arity, which could not work: the expression and
    its operands live in different attributes, so there was nothing to match
    within one. The link is the node map, and it binds all 52 expressions in the
    shipped file, 49 of them with one slot per placeholder.

    The three that differ are diagnosed rather than absorbed:

    * ``xA'`` -- one placeholder, two slots. Not a binding failure: the ``A``
      handler drains the whole operand array rather than taking one element per
      placeholder, so its arity *is* the array length.
    * ``xDx=`` -- two placeholders, one slot. A genuine deficit; with one
      operand the ``=`` would underflow, which suggests dead config, though that
      is not established.
    * ``xDx>xx<`` -- four placeholders, three slots, and slot 0 never written.
      The only port-0 group in the file whose slots are not dense from zero.
    """
    gs = graphs(d)
    attr2node, _ = node_map(d)
    out = []
    for j, (o, path, vals) in enumerate(attributes(d)):
        for vo, c, b in vals:
            if c != 0x23:
                continue
            text = _string_value(b).decode("latin1")
            if not text:
                continue
            gi_nd = attr2node.get(j)
            slots = operand_slots(d, *gi_nd, _graphs=gs) if gi_nd else []
            out.append((j, vo, text, slots))
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
        if not path:
            # The final run has no path: the file ends on block framing rather
            # than on another trailing list. Its records are real (a graph plus
            # a code-9), but they name no object, so there is nothing to key on.
            continue
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
    prev = None
    while o + 2 <= n:
        code = struct.unpack_from("<H", d, o)[0]
        # Must track `prev` exactly as records() does: without it a bare code-3
        # block tag is read as a 12-byte path list and the walk desynchronises
        # nine records in.
        ln = body_len(d, code, o + 2, prev)
        if ln is None or o + 2 + ln > n:
            return out, o, code
        out.append((o, code, 2 + ln))
        o += 2 + ln
        prev = code
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
RPN_VERIFIED = frozenset("+-*/%^~&|=<>?!aiomrxDdM")

#: Two-character tokens whose handlers have been read.
RPN_VERIFIED_DIGRAPHS = frozenset(("<<", ">>", "<=", ">=",
                                   "&&", "||", "MR", "M+",
                                   "i-", "i/", "r<", "r>", "m+"))

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


def _mask_bits(w):
    """The `r` handler's field mask: w ones, built one bit at a time.

    Read from the loop at 08906958-0890696E, which starts r8 at 0 and repeats
    ``add r8,r8 ; add #1,r8``. The entry test is a signed ``cmp/ge``, so w <= 0
    produces 0. Capped at 32 here because the register saturates there; on the
    real unit a w of 65536 or more simply spins.
    """
    return (1 << min(w, 32)) - 1 if w > 0 else 0


def _shld(x, s):
    """SH4 SHLD: dynamic *logical* shift, direction from the sign of s."""
    u = x & 0xFFFFFFFF
    if s >= 0:
        return (u << (s & 31)) & 0xFFFFFFFF
    k = (-s) & 31
    return 0 if k == 0 else (u >> k)


def _shad(x, s):
    """SH4 SHAD: dynamic *arithmetic* shift; x is signed, so the sign fills."""
    if s >= 0:
        return ((x & 0xFFFFFFFF) << (s & 31)) & 0xFFFFFFFF
    k = (-s) & 31
    if k == 0:
        return 0xFFFFFFFF if x < 0 else 0
    return (x >> k) & 0xFFFFFFFF


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

    The last five handlers, each named by its own trace string:

    ``!`` (``089067B6``, ``'!%d'``) is logical NOT, unary in place. It uses
    ``tst`` -- an exact ``== 0`` test -- where ``&&`` and ``||`` use ``cmp/pl``
    (``> 0``). So ``!`` is **not** the complement of this interpreter's own
    truthiness: ``!(-1)`` is 0, yet ``-1`` is false to ``&&``. Both readings
    come from the bytes; the inconsistency is the firmware's.

    ``i`` (``08906828``) is a three-way dispatch on the next character:

    * plain ``i`` (``089068A0``, ``'%d is inside %d, %d; '``) -- signed
      interval test ``second <= third <= top``, pop 3 push 1.
    * ``i-`` (``08906870``, ``'%d - %d   (invers); '``) -- ``top - second``.
    * ``i/`` (``0890683E``, ``'%d / %d   (invers); '``) -- ``top / second``.

    The two digraphs are exact mirrors of ``-`` and ``/``, which is what
    "invers" means here. ``i/`` **is** guarded against a zero divisor
    (``tst r5,r5`` at ``08906850`` yields 0), where plain ``/`` is not.

    ``o`` (``089068E0``, ``'%d is outside %d, %d; '``) is the exact complement
    of plain ``i``.

    ``r`` (``0890692E``) is a barrel **rotate** of ``third`` within a
    ``top``-bit field by ``second`` places -- ``r>`` right, ``r<`` left --
    despite trace strings reading ``'round > %d  between %d and %d; '``. The
    format string is misleading; the machine code rotates, which was confirmed
    numerically against an instruction-level simulation over the whole domain.
    A lone ``r`` followed by neither ``<`` nor ``>`` is a one-character no-op.

    ``m`` / ``m+`` (``08906B20``) are **arming prefixes, not value operators**.
    The handler touches no stack slot and no depth; it sets a mode byte at
    ``[r14+80]`` (``m+`` -> accumulate, ``m`` -> assign) that the memory side
    effect of ``i``/``o`` at ``08905CBC`` consults when updating the memory
    register at ``ctx+16``. Only the value stack is modelled here, so they are
    correctly no-ops for the returned value.

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
    mem_mode = 4                # [r14+80], 4 == disarmed; set at 08906218
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
        if tok == "!":
            if not st:
                raise ValueError("stack underflow at '!' in %r" % expr)
            # Logical NOT, unary in place: `tst r1,r1` at 089067C2 then
            # `movt r7` in the delay slot at 089067C6, and the unary epilogue
            # at 08906B1C writes r7 back to @(4,r9). Named by '!%d'.
            # NOTE the asymmetry with && and ||: this is `tst`, an exact ==0
            # test, where those use cmp/pl (">0"). So '!' is NOT the complement
            # of this interpreter's truthiness -- !(-1) is 0, yet -1 is false
            # to &&. Both readings are from the bytes; the inconsistency is the
            # firmware's, not ours.
            st.append(int(_sx(st.pop()) == 0))
            continue
        if tok in ("i", "o"):
            if len(st) < 3:
                raise ValueError("stack underflow at %r in %r" % (tok, expr))
            top = st.pop()
            second = st.pop()
            third = st.pop()
            # Signed three-operand interval predicates, exact complements.
            # 'i' 089068A0, named '%d is inside %d, %d; '  (cmp/gt 089068B6,
            # cmp/ge 089068BC). 'o' 089068E0, '%d is outside %d, %d; '.
            # The result is stored to @r10, the third slot, and depth drops by
            # two -- pop 3, push 1.
            inside = second <= third <= top
            st.append(int(inside if tok == "i" else not inside))
            continue
        if tok in ("i-", "i/"):
            if len(st) < 2:
                raise ValueError("stack underflow at %r in %r" % (tok, expr))
            top = st.pop()
            second = st.pop()
            # The "invers" digraphs: exact mirrors of '-' and '/'.
            # i- 08906870: `sub r2,r1` with r1=top, r2=second -> top - second.
            # i/ 0890683E: dividend r4 = top, divisor r5 = second.
            if tok == "i-":
                st.append(_sx(top - second))
            elif second == 0:
                # Unlike plain '/', this one IS guarded: `tst r5,r5` at
                # 08906850 skips the division, leaving second (which is 0) in
                # place, so the result is 0 rather than a trap.
                st.append(0)
            else:
                st.append(_sx(_trunc_div(top, second)))
            continue
        if tok in ("r<", "r>"):
            if len(st) < 3:
                raise ValueError("stack underflow at %r in %r" % (tok, expr))
            top = st.pop()          # w: field width in bits
            second = st.pop()       # n: rotate amount
            third = st.pop()        # x: the value
            # Barrel ROTATE of x within a w-bit field -- despite the trace
            # strings reading 'round > %d  between %d and %d; '. The format
            # string is misleading; the machine code rotates. Verified
            # numerically against an instruction-level simulation of
            # 08906946-0890698A and 0890699E-089069E4 over the full domain.
            m = _mask_bits(top)
            if tok == "r>":
                v = (_shld(third, top - second) & m) | _shad(third, -second)
            else:
                v = (_shld(third, second) & m) | _shad(third, second - top)
            st.append(_sx(v))
            continue
        if tok in ("m", "m+"):
            # Arming prefixes, NOT value operators: the handler at 08906B20
            # reads and writes no stack slot and never touches depth. It sets a
            # mode byte at [r14+80] -- 'm+' -> 0 (accumulate), 'm' -> 1 (assign)
            # -- which the memory side effect of 'i'/'o' at 08905CBC later
            # consults to update the memory register at ctx+16. Only the value
            # stack is modelled here, so this is correctly a no-op; the mode is
            # recorded for the caller.
            mem_mode = 0 if tok == "m+" else 1
            continue
        if tok == "r":
            # A lone 'r' -- next character neither '<' nor '>' -- takes the
            # bf/s at 08906942 to 0890692A, which is `bra 08906c1a` straight
            # into the shared loop tail. No stack read, no write, no depth
            # change: a one-character no-op. Only the digraphs do work.
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
