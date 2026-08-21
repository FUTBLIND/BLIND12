"""
aptread.py - read the ActionScript inside an Apt screen.

THE FORMAT, reversed here
-------------------------
An Apt SCREEN is TWO files inside its .big:

    entry "0"  magic "Apt Data:1:7:4"     movie + ACTION BYTECODE
    entry "1"  magic "Apt constant file"  the NAME POOL

Every earlier string dump in this project read the POOL. A pool lists what a
screen MENTIONS and contains no logic at all, so conclusions of the form "these
symbols appear in this order, therefore the call order is X" were never valid.
The logic is in the Data file, and it indexes the pool by number - so both
files are needed to read anything.

Data file layout:

    0x000010  import records (16 bytes: path off, dotted-name off, type, 0)
    0x000384  export table   (8 bytes: name off, character id)
    0x000474  character table, indexed by id -> character record offset
              character record: type, magic 0x09876543, ..., frame list
              frame record: array of tagged blocks; TAG 8 = init actions,
              carrying (character id, bytecode offset)

Function records inside an action block:

    8e 00 00 00 | nameoff | 0 | params | nameoff | CODE LENGTH | SENTINEL | code
    ^DefineFunction2                                             ^ 32 54 76 98
                                                                   78 56 34 12

BYTECODE ENCODING - not SWF
    Apt does NOT use SWF's `op + uint16 length + body`. Operand width is fixed
    per opcode:
        0xA2 A3 A4 AE AF B2 B5 B9   1-byte CONSTANT POOL INDEX
        0x87 8E 99 9D               2-byte operand (branch offsets, registers)
        everything else             no operand

    Those widths were derived statistically, not guessed: across the action
    blocks, 0xb9 is followed by 01/02/03/04/05 in 3178 samples while 0xa3 is
    followed by all 256 values - the signature of a 1-byte index. Assuming the
    SWF layout is why every earlier decode produced noise.

STILL INCOMPLETE
    Several opcodes remain unnamed (op59, op5a, op73, op76...) and a stream can
    desync after a branch, which shows up as a run of op_00. Names resolved from
    the pool are reliable; exact control flow past a desync is not. Output says
    op_XX rather than inventing a mnemonic.

USAGE
    python aptread.py <datafile> --pool <constfile>
    python aptread.py <datafile> --pool <constfile> --find pushScreen
    python aptread.py <datafile> --pool <constfile> --funcs
"""
import re
import struct
import sys

SENTINEL = bytes.fromhex("3254769878563412")

# 1-byte pool index. 0xB0 was MISSING and that mattered: without it the two
# bytes after it decoded as phantom "op_71 / op_72" instructions, and those are
# not opcodes at all - they are POOL INDICES naming the two continuations:
#     pool[0x71] = 'ContinueToCreateClub'   pool[0x72] = 'ContinueLogIn'
# Its giveaway in the corpus was that the bytes FOLLOWING op_B0 were junk
# (op_FD, op_FE, op_03) - the signature of an unconsumed operand.
W1 = {0xA2, 0xA4, 0xAE, 0xAF, 0xB0, 0xB2, 0xB5, 0xB9}
W2C = {0xA3}                                      # 2-byte POOL INDEX

# 4-BYTE OPERAND, ALIGNED TO A 4-BYTE BOUNDARY.
#
# These were decoded as unaligned 2-byte operands, which is the second half of
# the desync bug (0x8E's variable record was the first). The giveaway was that
# If / StoreReg / Jump were overwhelmingly the opcodes sitting immediately
# before a run of op_00 - those "instructions" were the PADDING and the high
# half of a 32-bit operand being read as code.
#
# Raw bytes settle it. Where the operand happens to be aligned already:
#     99 03 00 00 00 59        Jump 3,       next op 0x59
#     9d 0f 00 00 00 a2        If 15,        next op 0xa2
# and where it is not, the gap is padding, not instructions:
#     9d 00 00 16 00 00 00 59  pad 2 -> If 22,       next op 0x59
#     99 00 00 27 00 00 00 b9  pad 2 -> Jump 39,     next op 0xb9
#     87 00 02 00 00 00 17     pad 1 -> StoreReg 2,  next op 0x17
#
# So the operand starts at align4(p+1) and the next opcode at align4(p+1)+4 -
# the same alignment rule the 0x8E record follows, which is what makes this
# consistent rather than a coincidence.
W4A = {0x87, 0x99, 0x9D}
W2 = set()                                        # nothing is a bare 2-byte operand

# 0xA3 carries a TWO-byte index, proven empirically: in futmain (8522 names)
# pushScreen=1272, GAMEHUB=1274 and NEW_ITEMS=1259 each appear as a 16-bit LE
# value immediately preceded by 0xa3. Treating it as 1-byte is why a large pool
# decoded to nothing while a small one (883 names, all indices < 256) appeared
# to work - the small pool never exercised the difference.

NAMES = {
    0x12: "Not", 0x17: "Pop", 0x26: "Trace", 0x3D: "CallFunction",
    0x3E: "Return", 0x40: "NewObject", 0x49: "Equals2", 0x4C: "PushDup",
    0x4E: "GetMember", 0x4F: "SetMember", 0x52: "CallMethod",
    0x87: "StoreReg", 0x8E: "DefineFn", 0x99: "Jump", 0x9D: "If",
    0xA2: "PushC", 0xA3: "PushC", 0xA4: "PushC", 0xAE: "PushC",
    0xAF: "PushC", 0xB2: "GetProp", 0xB5: "PushC", 0xB9: "PushReg",
}


def read_const_file(d):
    """Names from an 'Apt constant file': 8-byte {type, offset} records."""
    if not d.startswith(b"Apt constant file"):
        return None
    names, p = [], 0x20
    nul = bytes([0])
    while p + 8 <= len(d):
        typ, off = struct.unpack_from("<II", d, p)
        p += 8
        if typ != 1 or not (0 < off < len(d)):
            continue
        e = d.find(nul, off)
        if e < 0 or e - off > 200:
            continue
        names.append(d[off:e].decode("latin-1", "replace"))
    return names


def functions(d):
    """-> [(code_offset, code_len)] for every function record."""
    out = []
    for m in re.finditer(re.escape(SENTINEL), d):
        h = m.start()
        if h < 4:
            continue
        ln = struct.unpack_from("<I", d, h - 4)[0]
        if 0 < ln < len(d) - h:
            out.append((h + 8, ln))
    return out


def defn_record(d, p):
    """Parse a 0x8E DefineFunction2 record starting at the opcode byte.

    THE DESYNC BUG THIS FIXES
    -------------------------
    0x8E was decoded as a plain 2-byte operand, which is wrong: it introduces a
    VARIABLE-LENGTH record, so every stream containing one drifted from that
    point on and produced the long runs of op_00 that made control flow
    unreadable. fcc_login happened to survive because its functions were split
    on the sentinel and the decode never crossed a DefineFn body; futuxtransition
    does cross one, which is why THAT file - the legacy->UX bridge - was the one
    that could not be read.

    Layout, derived byte-exactly from futuxtransition (10 records) and checked
    against every sentinel position in the file:

        8e | PAD TO 4-BYTE BOUNDARY | u32 nameoff | u32 nparams | u16 | u16
           | u32 nameoff2 | u32 codelen | SENTINEL(8) | code[codelen]

    The padding is what made the header look inconsistent - 21 bytes when 0x8e
    sat on an even offset, 22 on an odd one. It is alignment, not a variable
    field. Two worked examples:

        8e@0272 -> aligned 0x274, codelen  17, sentinel lands exactly at 0x288
        8e@02c1 -> aligned 0x2c4, nparams 2, codelen 204, sentinel exactly 0x2d8

    Returns (body_start, codelen, nparams) or None if this is not a valid
    record - the caller then falls back to the old 2-byte read rather than
    guessing, so a misfire degrades to the previous behaviour instead of
    inventing structure.
    """
    q = (p + 1 + 3) & ~3
    if q + 28 > len(d):
        return None
    nameoff, nparams, _a, _b, _n2, codelen = struct.unpack_from("<IIHHII", d, q)
    if d[q + 20:q + 28] != SENTINEL:
        return None
    if codelen <= 0 or q + 28 + codelen > len(d):
        return None
    return q + 28, codelen, nparams


def decode(d, off, n, pool, descend=False):
    """-> [(offset, mnemonic, operand, resolved_name)]

    descend=False skips a DefineFn's BODY (it is decoded separately as its own
    function record, so descending would emit it twice); the record is still
    consumed correctly either way, which is what stops the drift.
    """
    out, p, end = [], off, min(off + n, len(d))
    while p < end:
        op = d[p]
        nm = NAMES.get(op, "op_%02X" % op)
        if op == 0x8E:
            rec = defn_record(d, p)
            if rec:
                body, codelen, nparams = rec
                out.append((p, "DefineFn", codelen,
                            "<%d params, %d bytes>" % (nparams, codelen)))
                p = body if descend else body + codelen
                continue
        if op in W4A:
            q = (p + 1 + 3) & ~3
            if q + 4 > end:
                out.append((p, nm, None, None))
                p += 1
                continue
            a = struct.unpack_from("<i", d, q)[0]
            out.append((p, nm, a, None))
            p = q + 4
            continue
        if op in W1 and p + 1 < end:
            a = d[p + 1]
            out.append((p, nm, a, pool[a] if pool and a < len(pool) else None))
            p += 2
        elif op in W2C and p + 2 < end:
            a = struct.unpack_from("<H", d, p + 1)[0]
            out.append((p, nm, a, pool[a] if pool and a < len(pool) else None))
            p += 3
        elif op in W2 and p + 2 < end:
            a = struct.unpack_from("<h", d, p + 1)[0]
            out.append((p, nm, a, None))
            p += 3
        else:
            out.append((p, nm, None, None))
            p += 1
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    d = open(sys.argv[1], "rb").read()
    pool = None
    if "--pool" in sys.argv:
        pool = read_const_file(open(sys.argv[sys.argv.index("--pool") + 1],
                                    "rb").read())
    if not d.startswith(b"Apt Data"):
        print("not an 'Apt Data' file - pass the entry whose magic is Apt Data")
        return 1
    fns = functions(d)
    print("pool: %s names   function records: %d"
          % (len(pool) if pool else "none", len(fns)))

    if "--funcs" in sys.argv:
        for off, ln in fns:
            print("   0x%06x  %5d bytes" % (off, ln))
        return 0

    want = (sys.argv[sys.argv.index("--find") + 1]
            if "--find" in sys.argv else None)
    if want is None:
        off, ln = fns[0]
        for o, mn, a, nmv in decode(d, off, ln, pool):
            print("   %06x  %-10s %s" % (o, mn, repr(nmv) if nmv else
                                         ("" if a is None else a)))
        return 0

    hits = 0
    for off, ln in fns:
        code = decode(d, off, ln, pool)
        idx = [i for i, (o, mn, a, nmv) in enumerate(code)
               if nmv and want.lower() in nmv.lower()]
        if not idx:
            continue
        hits += 1
        print()
        print("=== function at 0x%06x (%d bytes) ===" % (off, ln))
        for i in idx:
            lo, hi = max(0, i - 14), min(len(code), i + 10)
            for j in range(lo, hi):
                o, mn, a, nmv = code[j]
                mark = "  <==" if j == i else ""
                print("   %06x  %-10s %s%s"
                      % (o, mn, repr(nmv) if nmv else
                         ("" if a is None else a), mark))
            print("   ---")
    print()
    print("%d function(s) reference %r" % (hits, want))
    return 0


if __name__ == "__main__":
    sys.exit(main())
