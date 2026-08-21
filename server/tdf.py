"""
Minimal EA Blaze TDF (Tag-Delimited Format) encoder/decoder.
Rules verified against tdf.js (github.com/ploxxxy/tdf.js) and cross-checked
against a real captured FIFA 12 redirector request (first field tag decoded
to "BSDK" = BlazeSDK version, matching the plaintext value "3.9.4.3").
"""
import struct


class TDFType:
    INTEGER = 0
    STRING = 1
    BLOB = 2
    STRUCT = 3
    LIST = 4
    DICTIONARY = 5
    UNION = 6
    INTEGER_LIST = 7
    INT_VECTOR2 = 8
    INT_VECTOR3 = 9


def encode_label(tag):
    """4-char tag name -> 3 bytes."""
    tag = tag.upper()
    if len(tag) != 4:
        raise ValueError("tag must be 4 chars")
    encoded = 0
    for i, ch in enumerate(tag):
        if ch == " ":
            continue
        encoded |= (ord(ch) - 32) << (6 * (3 - i))
    return bytes([(encoded >> 16) & 0xFF, (encoded >> 8) & 0xFF, encoded & 0xFF])


def decode_label(b3):
    encoded = (b3[0] << 16) | (b3[1] << 8) | b3[2]
    out = [0, 0, 0, 0]
    for i in range(3, -1, -1):
        six = encoded & 0x3F
        out[i] = (six + 32) if six else 0x20
        encoded >>= 6
    return bytes(out).decode("ascii")


def compress_integer(value):
    """Variable-length integer encoding used by Blaze TDF."""
    value = int(value) & 0xFFFFFFFF
    out = bytearray()
    if value < 0x40:
        out.append(value & 0xFF)
    else:
        cur = (value & 0x3F) | 0x80
        out.append(cur)
        shift = value >> 6
        while shift >= 0x80:
            out.append((shift & 0x7F) | 0x80)
            shift >>= 7
        out.append(shift)
    return bytes(out)


def decompress_integer(data, off):
    b = data[off]
    off += 1
    result = b & 0x3F
    shift = 6
    while b & 0x80:
        b = data[off]
        off += 1
        result |= (b & 0x7F) << shift
        shift += 7
    return result & 0xFFFFFFFF, off


def write_string(s):
    raw = s.encode("utf8") + b"\x00"
    return compress_integer(len(raw)) + raw


def read_string(data, off):
    length, off = decompress_integer(data, off)
    if length == 0:
        return "", off
    raw = data[off:off + length - 1]
    off += length  # skip string bytes + null terminator
    return raw.decode("utf8", errors="replace"), off


# ---- field builders (write side) ----

def field_prefix(tag, type_):
    return encode_label(tag) + bytes([type_])


def field_integer(tag, value):
    return field_prefix(tag, TDFType.INTEGER) + compress_integer(value)


def field_string(tag, value):
    return field_prefix(tag, TDFType.STRING) + write_string(value)


def field_struct(tag, fields_bytes):
    """fields_bytes: already-concatenated bytes of child fields."""
    return field_prefix(tag, TDFType.STRUCT) + fields_bytes + b"\x00"


def field_blob(tag, value: bytes):
    return field_prefix(tag, TDFType.BLOB) + compress_integer(len(value)) + value


def field_union(tag, union_type, inner_field_bytes):
    """inner_field_bytes: the write() bytes of the wrapped TDF (includes its own tag+type+value)."""
    return field_prefix(tag, TDFType.UNION) + bytes([union_type]) + inner_field_bytes


def struct_no_tag(fields_bytes):
    """Top-level struct body (no tag/type prefix, no outer terminator needed by caller - caller adds it)."""
    return fields_bytes + b"\x00"


def _encode_scalar(type_, value):
    if type_ == TDFType.INTEGER:
        return compress_integer(value)
    if type_ == TDFType.STRING:
        return write_string(value)
    if type_ == TDFType.STRUCT:
        # value: bytes of already-concatenated child fields (no tag/type prefix, no terminator)
        return value + b"\x00"
    raise NotImplementedError(f"scalar encode for type {type_} not implemented")


def field_integer_list(tag, values):
    body = compress_integer(len(values)) + b"".join(compress_integer(v) for v in values)
    return field_prefix(tag, TDFType.INTEGER_LIST) + body


def field_list(tag, subtype, values):
    """values: list of already-prepared items matching subtype (raw structs bytes, strings, ints)."""
    body = bytes([subtype]) + compress_integer(len(values))
    for v in values:
        body += _encode_scalar(subtype, v)
    return field_prefix(tag, TDFType.LIST) + body


def field_dictionary(tag, key_type, value_type, items):
    """items: list of (key, value) tuples."""
    body = bytes([key_type, value_type]) + compress_integer(len(items))
    for k, v in items:
        body += _encode_scalar(key_type, k)
        body += _encode_scalar(value_type, v)
    return field_prefix(tag, TDFType.DICTIONARY) + body


# ---- decode side (for inspecting captured packets) ----

def decode_value(data, off, type_):
    if type_ == TDFType.INTEGER:
        val, off = decompress_integer(data, off)
        return val, off
    if type_ == TDFType.STRING:
        val, off = read_string(data, off)
        return val, off
    if type_ == TDFType.BLOB:
        length, off = decompress_integer(data, off)
        val = data[off:off + length]
        off += length
        return val, off
    if type_ == TDFType.STRUCT:
        fields = []
        while data[off] != 0:
            f, off = decode_field(data, off)
            fields.append(f)
        off += 1
        return fields, off
    if type_ == TDFType.UNION:
        union_type = data[off]
        off += 1
        if union_type == 0x7F:
            return (union_type, None), off
        f, off = decode_field(data, off)
        return (union_type, f), off
    if type_ == TDFType.INTEGER_LIST:
        length, off = decompress_integer(data, off)
        vals = []
        for _ in range(length):
            v, off = decompress_integer(data, off)
            vals.append(v)
        return vals, off
    if type_ == TDFType.LIST:
        subtype = data[off]
        off += 1
        length, off = decompress_integer(data, off)
        vals = []
        for _ in range(length):
            v, off = decode_value(data, off, subtype)
            vals.append(v)
        return (subtype, vals), off
    if type_ == TDFType.DICTIONARY:
        key_type = data[off]
        value_type = data[off + 1]
        off += 2
        length, off = decompress_integer(data, off)
        items = []
        for _ in range(length):
            k, off = decode_value(data, off, key_type)
            v, off = decode_value(data, off, value_type)
            items.append((k, v))
        return (key_type, value_type, items), off
    raise NotImplementedError(f"decode for type {type_} not implemented")


def decode_field(data, off):
    tag = decode_label(data[off:off + 3])
    type_ = data[off + 3]
    off += 4
    val, off = decode_value(data, off, type_)
    return (tag, type_, val), off


def decode_struct_body(data, off=0):
    fields = []
    while off < len(data) and data[off] != 0:
        f, off = decode_field(data, off)
        fields.append(f)
    return fields, off
