"""
Extracts EA "chunkzip"-wrapped FUT UI files (as found inside cards_patch.big):
  chunkzip header (raw DEFLATE payload) -> nested BIGF mini-archive -> named entries
    - one "Apt Data:..." bytecode entry
    - one "Apt constant file" string-table entry
"""
import struct
import zlib
import re
import sys
import os


def parse_bigf(data: bytes):
    if data[:4] != b"BIGF":
        raise ValueError(f"not a BIGF archive: {data[:4]!r}")
    num_files = struct.unpack(">I", data[8:12])[0]
    pos = 16
    entries = []
    for _ in range(num_files):
        offset, size = struct.unpack(">II", data[pos:pos+8])
        pos += 8
        name_end = data.index(b"\x00", pos)
        name = data[pos:name_end].decode("ascii", errors="replace")
        pos = name_end + 1
        entries.append((name, offset, size))
    return entries


def extract_chunkzip(path: str):
    data = open(path, "rb").read()
    if data[:8] != b"chunkzip":
        raise ValueError(f"not a chunkzip file: {data[:8]!r}")
    total_size = struct.unpack(">I", data[0x0C:0x10])[0]
    chunk_size = struct.unpack(">I", data[0x10:0x14])[0]
    num_chunks = struct.unpack(">I", data[0x14:0x18])[0]

    out = bytearray()
    pos = 0x28
    for i in range(num_chunks):
        remaining = total_size - len(out)
        expected = min(chunk_size, remaining)
        # header layout is consistent (comp_size BE u32, flag==1 BE u32) but the
        # gap before it varies chunk-to-chunk by a few bytes of unexplained
        # padding, so search a small window rather than assume a fixed offset.
        found = False
        for shift in range(0, 64):
            p = pos + shift
            comp_size = struct.unpack(">I", data[p:p+4])[0]
            flag = struct.unpack(">I", data[p+4:p+8])[0]
            if flag != 1 or comp_size <= 0 or p + 8 + comp_size > len(data):
                continue
            try:
                chunk_out = zlib.decompress(data[p+8:p+8+comp_size], -15)
            except zlib.error:
                continue
            if len(chunk_out) == expected:
                out += chunk_out
                pos = p + 8 + comp_size
                found = True
                break
        if not found:
            raise ValueError(f"could not locate chunk {i} header near offset {pos}")

    assert len(out) == total_size, f"size mismatch {len(out)} != {total_size}"
    return bytes(out)


def strings_of(data: bytes, minlen=4):
    return [m.decode("ascii") for m in re.findall(rb"[\x20-\x7e]{%d,}" % minlen, data)]


if __name__ == "__main__":
    src = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(src)
    base = os.path.splitext(os.path.basename(src))[0]

    decompressed = extract_chunkzip(src)
    entries = parse_bigf(decompressed)
    print(f"{src}: {len(entries)} entries")
    for name, offset, size in entries:
        print(f"  {name!r} offset={offset} size={size}")
        if size == 0:
            continue
        chunk = decompressed[offset:offset+size]
        safe_name = name.replace(":", "_").replace("/", "_") or "unnamed"
        out_path = os.path.join(outdir, f"{base}_{safe_name}.bin")
        with open(out_path, "wb") as f:
            f.write(chunk)
        strs = strings_of(chunk)
        strpath = os.path.join(outdir, f"{base}_{safe_name}_strings.txt")
        with open(strpath, "w", encoding="utf-8") as f:
            f.write("\n".join(strs))
        print(f"    -> {out_path} ({len(strs)} strings extracted)")
