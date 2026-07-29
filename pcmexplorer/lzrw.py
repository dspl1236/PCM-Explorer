"""HBM5 compressed payloads -- Harman's LZRW codec.

The ``.mmi`` HMI files mark some payloads compressed (directory byte +11).
PCM3Reload names the codec in its own strings -- ``HBLZRW2Compression.cpp``
and ``HBLZRW3ACompression.cpp`` -- so the family is Ross Williams' LZRW, and
the binary even carries LZRW3's ``123456789012345678`` hash-table seed string.

It is **stock LZRW2**, and Harman's class name is truthful: the routine at
``0x088d0550`` matches the published ``lzrw2.c`` line for line -- only slot 0
seeded with ``START_STRING_18``, ``phrase[next++] = p_dst`` on *every* item,
``next &= 0xFFF`` on the control-word refill, the 16/1 unroll at ``end-32``.
(``HBLZRW3ACompression.cpp`` is a genuine second codec at ``0x088d5cfc``; it
is not what these payloads use.)

Worth stating because it was got wrong twice.  This looks like "LZRW3-A with a
sequentially-filled table instead of a hash" if you derive it from the data
alone -- which is a byte-accurate description of LZRW2, arrived at without
naming it.  And the ``123456789012345678`` in the binary is not a self-test
vector or an LZRW3 hash seed: it is the uninitialised-slot seed, which is why
decoding with the wrong variant yields text with ``12345678`` spliced through
it.  Only the container framing was ever Harman's; the algorithm is unmodified
public-domain code.

## Container framing

A compressed payload is::

    varint  compressed length   (bytes of LZ stream that follow the header)
    varint  uncompressed length (exact -- this is the decode oracle)
    u8      codec
    ...     LZ stream

Two codec bytes carry this coder: 0x20 (1227 payloads) and 0xa0 (3583).  They
decode through the identical code path, so the high bit is a flag about the
payload, not about the bitstream -- 0xa0 is only ever binary, while 0x20
carries all 792 text payloads and 435 binary ones.

Five other codec bytes exist -- 0x00, 0x01, 0x02, 0x80, 0x81, 402 payloads in
all, almost entirely in ``moccaV2Target.mmi`` -- and they are **not** this
format.  Their framing is the same (the declared compressed length matches the
block exactly) but the stream is not: sweeping minimum match length, the split
of the two item bytes and the stream start offset over every combination gets
no variant past chance level, and a brute force over start offsets 0-15 found
only false positives leaking the seed string.

Nor is the third byte a codec byte for them: ``0x02 & 3 == 2`` reaches the
dispatcher's *fatal* arm at ``0x088d06ec``, and one such block declares 16
stored bytes expanding to 1,451, which no LZ77 with an 18-byte maximum match
can do.  So the second field is not an uncompressed length either, and these
are a different record shape rather than a different compressor.  What they
are is unresolved.  ``decompress`` raises on them rather than returning a
plausible-looking prefix.

Varints are the same UTF-8-shaped ones the rest of the container uses
(see ``hbm5.read_varint``).  The decompressed bytes are the payload proper --
for a string payload that is again ``varint length`` followed by UTF-8, which
is why the first decoded byte of a short string blob is its own length.

## LZ stream

Groups of 16 items, each group preceded by a little-endian 16-bit control
word, LSB first, one bit per item: 0 = literal, 1 = copy.  A group may be cut
short by the end of the stream (the declared uncompressed length is what
terminates the decode; the last control word's unused bits are ignored).

A literal is one byte, emitted as-is.  A copy is two bytes::

    b0, b1  ->  length = (b0 & 0x0F) + 3          3..18
                index  = ((b0 & 0xF0) << 4) | b1  0..4095

``index`` selects an entry from a 4096-entry table of positions in the output
produced so far; the copy takes ``length`` bytes from there.  Both sides fill
that table identically, so the decoder must reproduce the rule exactly:

  * a literal increments a pending-literal counter; when it reaches 3, the
    position three bytes back is appended to the table and the counter drops
    back to 2 (so a run of literals enters every position except the last two);
  * a copy first flushes those trailing literals into the table (one entry, or
    two if two were pending), then reads its source from the table, then
    appends the position at which the copy started.

That flush order is the one place this differs from LZRW3-A beyond the index
itself: stock LZRW3-A flushes *after* the copy, because it cannot hash bytes
it has not produced.  Here the flush has to happen first -- copies do point at
the two literals immediately behind them.  The difference is measurable, not a
matter of taste: flushing afterwards leaves 417 of 5212 payloads reading a
slot that was never written, flushing first leaves 4.

Slots never written point at the constant string ``123456789012345678``, as
in LZRW3-A -- so the two sides stay in step without a validity test.  A decode
that reads one is not necessarily wrong, merely unusual.

## Trusting the result

The header declares the uncompressed length, so a decode either lands on it
exactly or it is wrong; ``decompress`` treats a mismatch as an error rather
than returning a plausible-looking prefix.  Over the 21 ``.mmi`` files in
``D:\\PCM\\mmi_work`` that is 4810 of 4810 payloads carrying this codec --
100.00%, 22.3 MB of payload out of 13.5 MB of blocks.

Two further checks, both independent of the length:

  * 792 of those payloads decode to a varint length followed by text.  All 792
    have an inner length that accounts for the decoded bytes exactly *and*
    decode as UTF-8, in German, Russian, Arabic, Chinese and the rest.  A copy
    landing one byte off would break the inner length or the encoding.
  * across all 4810, not one copy reads a table slot that was never written.
    An unwritten slot is legal (it yields the seed string) but pointless, so
    the encoder should never emit one -- and it never does.  That is what
    pins the table-fill rule down: with the literal flush in LZRW3-A's order
    instead, 417 payloads read unwritten slots and 36 strings come out with
    ``123456`` embedded in them.
"""

# LZRW3-A's hash-table seed.  Present verbatim in PCM3Reload, immediately
# before both HBLZRW2Compression.cpp and HBLZRW3ACompression.cpp.
START_STRING_18 = b"123456789012345678"

TABLE_LENGTH = 4096          # the copy item's index field is 12 bits
MIN_MATCH = 3                # length = (b0 & 0x0F) + 3, so 3..18


class LzrwError(Exception):
    """The stream did not decode -- truncated, or not this codec."""


def read_varint(data, off):
    """(value, next_offset).  UTF-8-shaped: 1, 2 or 3 bytes.

    Duplicated from ``hbm5`` so this module stands alone.
    """
    b = data[off]
    if b < 0x80:
        return b, off + 1
    if (b & 0xC0) == 0x80:
        return ((b & 0x3F) << 8) | data[off + 1], off + 2
    return ((b & 0x3F) << 16) | (data[off + 1] << 8) | data[off + 2], off + 3


def parse_header(blob, off=0):
    """(compressed_len, uncompressed_len, codec, stream_offset)."""
    try:
        clen, p = read_varint(blob, off)
        ulen, p = read_varint(blob, p)
        codec = blob[p]
    except IndexError:
        raise LzrwError("blob too short for a header")
    return clen, ulen, codec, p + 1


def decompress_stream(src, out_len=None):
    """Decode one raw LZ stream (no header).  Returns a bytearray.

    ``out_len`` stops the decode as soon as that many bytes exist, which is
    what the format intends -- the final group is normally cut short.
    """
    out = bytearray()
    # Table of source positions.  A negative entry means "the seed string",
    # which is what LZRW3-A initialises every slot to.
    table = [-1] * TABLE_LENGTH
    write = 0                     # next table slot to fill

    src_len = len(src)
    p = 0
    control = 1                   # 1 == "load a new control word"
    literals = 0                  # pending literals not yet entered

    while p < src_len:
        if out_len is not None and len(out) >= out_len:
            break
        if control == 1:
            if p + 2 > src_len:
                break
            control = 0x10000 | src[p] | (src[p + 1] << 8)
            p += 2
        while control != 1 and p < src_len:
            if out_len is not None and len(out) >= out_len:
                break
            if control & 1:
                # -- copy item --
                if p + 2 > src_len:
                    raise LzrwError("truncated copy item at %d" % p)
                b0 = src[p]
                index = ((b0 & 0xF0) << 4) | src[p + 1]
                p += 2
                length = (b0 & 0x0F) + MIN_MATCH
                start = len(out)
                # The postponed literal positions are entered BEFORE the copy
                # reads the table -- the encoder counts them, and a copy is
                # allowed to point at the literals immediately behind it.
                # (Doing this afterwards, as stock LZRW3-A does, leaves 417 of
                # the 5212 payloads reading an unwritten slot; doing it first
                # leaves 4.)
                if literals:
                    table[write] = start - literals
                    write = (write + 1) % TABLE_LENGTH
                    if literals == 2:
                        table[write] = start - 1
                        write = (write + 1) % TABLE_LENGTH
                    literals = 0
                source = table[index]
                if source < 0:
                    for k in range(length):
                        out.append(START_STRING_18[k % len(START_STRING_18)])
                else:
                    for k in range(length):
                        out.append(out[source + k])
                table[write] = start
                write = (write + 1) % TABLE_LENGTH
            else:
                # -- literal item --
                out.append(src[p])
                p += 1
                literals += 1
                if literals == 3:
                    table[write] = len(out) - 3
                    write = (write + 1) % TABLE_LENGTH
                    literals = 2
            control >>= 1
    return out


def decompress(blob, off=0):
    """Decode a framed payload.  Returns the decompressed bytes.

    Raises ``LzrwError`` unless the result is exactly the length the header
    declares -- there is no reason to hand back a decode that failed its own
    self-check.
    """
    clen, ulen, codec, start = parse_header(blob, off)
    end = start + clen
    if end > len(blob):
        end = len(blob)          # block padding is sometimes trimmed
    out = decompress_stream(blob[start:end], ulen)
    if len(out) != ulen:
        raise LzrwError("decoded %d bytes, header declares %d" % (len(out), ulen))
    return bytes(out)


def looks_compressed(blob, off=0):
    """Cheap plausibility check -- does the header agree with the blob size?"""
    try:
        clen, ulen, codec, start = parse_header(blob, off)
    except LzrwError:
        return False
    return 0 < clen <= len(blob) - start + 16 and ulen >= clen


def decompress_string(blob, off=0):
    """A compressed *string* payload -> text.

    The decompressed bytes are themselves ``varint length`` + UTF-8, the same
    shape an uncompressed payload has.
    """
    raw = decompress(blob, off)
    ln, p = read_varint(raw, 0)
    if p + ln > len(raw):
        raise LzrwError("inner length %d overruns %d decoded bytes" % (ln, len(raw)))
    return raw[p:p + ln].decode("utf-8")


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:]:
        with open(path, "rb") as f:
            blob = f.read()
        clen, ulen, codec, start = parse_header(blob)
        try:
            out = decompress(blob)
            note = "ok"
        except LzrwError as exc:
            out = b""
            note = str(exc)
        print("%-34s clen=%-7d ulen=%-7d codec=0x%02x  %s"
              % (path.rsplit("/", 1)[-1], clen, ulen, codec, note))
        if out:
            try:
                print("    %r" % decompress_string(blob)[:200])
            except Exception:
                print("    binary, first bytes %r" % out[:48])
