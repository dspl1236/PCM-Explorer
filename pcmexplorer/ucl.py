"""UCL NRV2B decompression, pure Python.

QNX's mkifs can compress a boot image with UCL instead of LZO1X (startup header
flags1 compression bits = 0x0C). The Audi MMI 3G+ emergency image (ifs-emg) is
built that way. Same chunk chain as the LZO images -- a big-endian u16 length,
then that many compressed bytes -- each chunk one NRV2B stream.

This is the 8-bit-bit-buffer decoder (``ucl_nrv2b_decompress_8``), after UCL's
n2b_d.c. Read-only like everything else here: there is no compressor.
"""


def decompress(src):
    """Decompress one NRV2B stream; returns bytes. Raises ValueError on a stream
    that runs off its input or points before the start of its output."""
    out = bytearray()
    ip = 0
    bb = 0
    n = len(src)
    last_off = 1

    def bit():
        nonlocal bb, ip
        bb = (bb * 2) & 0x1FF
        if not bb & 0xFF:
            if ip >= n:
                raise ValueError("UCL stream runs past its end")
            bb = src[ip] * 2 + 1
            ip += 1
        return bb >> 8 & 1

    while True:
        while bit():
            if ip >= n:
                raise ValueError("UCL stream runs past its end")
            out.append(src[ip])
            ip += 1
        off = 1
        while True:
            off = off * 2 + bit()
            if bit():
                break
        if off == 2:
            off = last_off
        else:
            if ip >= n:
                raise ValueError("UCL stream runs past its end")
            off = ((off - 3) * 256 + src[ip]) & 0xFFFFFFFF
            ip += 1
            if off == 0xFFFFFFFF:
                break                          # end marker
            off += 1
            last_off = off
        length = bit()
        length = length * 2 + bit()
        if length == 0:
            length = 1
            while True:
                length = length * 2 + bit()
                if bit():
                    break
            length += 2
        length += off > 0xD00
        pos = len(out) - off
        if pos < 0:
            raise ValueError("UCL match points before the start of the output")
        for i in range(length + 1):
            out.append(out[pos + i])
    return bytes(out)
