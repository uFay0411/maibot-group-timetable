"""WakeUp native protocol subset, adapted from airline233/WakeUpDecoder.

SPDX-License-Identifier: Apache-2.0
Upstream revision: 577161c9ea8c34dddbcecd6ea9a702687c4ac8c4
Source: https://github.com/airline233/WakeUpDecoder
Modified 2026-10-25: retain only pure protocol helpers; remove APK parsing,
networking, diagnostic dumps and CLI; make zip truncation explicit.
License: licenses/WakeUpDecoder-APACHE-2.0.txt.
"""

from urllib.parse import quote_plus

import base64
import hashlib
import re
import secrets
import time

MAGIC = "8&%d*"       # Fixed marker in signA plaintext and final MD5 request signatures.


SIGN_A_KEY = "@fG2SuLA"  # Fixed 8-byte native-DES key used to encrypt/decrypt signA.


KEY_SALT = "@#AIjd83#@6B"  # Static salt used by native_get_key() to derive the RC4 request key.


IP = [
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
    56, 48, 40, 32, 24, 16, 8, 0, 58, 50, 42, 34, 26, 18, 10, 2,
    60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
]


FP = [
    39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
    37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28,
    35, 3, 43, 11, 51, 19, 59, 27, 34, 2, 42, 10, 50, 18, 58, 26,
    33, 1, 41, 9, 49, 17, 57, 25, 32, 0, 40, 8, 48, 16, 56, 24,
]


E = [
    31, 0, 1, 2, 3, 4, 3, 4, 5, 6, 7, 8,
    7, 8, 9, 10, 11, 12, 11, 12, 13, 14, 15, 16,
    15, 16, 17, 18, 19, 20, 19, 20, 21, 22, 23, 24,
    23, 24, 25, 26, 27, 28, 27, 28, 29, 30, 31, 0,
]


P = [
    15, 6, 19, 20, 28, 11, 27, 16, 0, 14, 22, 25, 4, 17, 30, 9,
    1, 7, 23, 13, 31, 26, 2, 8, 18, 12, 29, 5, 21, 10, 3, 24,
]


PC1 = [
    56, 48, 40, 32, 24, 16, 8, 0, 57, 49, 41, 33, 25, 17,
    9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35,
    62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21,
    13, 5, 60, 52, 44, 36, 28, 20, 12, 4, 27, 19, 11, 3,
]


PC2 = [
    13, 16, 10, 23, 0, 4, 2, 27, 14, 5, 20, 9,
    22, 18, 11, 3, 25, 7, 15, 6, 26, 19, 12, 1,
    40, 51, 30, 36, 46, 54, 29, 39, 50, 44, 32, 46,
    43, 48, 38, 55, 33, 52, 45, 41, 49, 35, 28, 31,
]


SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]


SBOX = [
    [
        [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7],
        [0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8],
        [4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0],
        [15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    ],
    [
        [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10],
        [3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5],
        [0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15],
        [13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    ],
    [
        [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8],
        [13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1],
        [13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7],
        [1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    ],
    [
        [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15],
        [13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9],
        [10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4],
        [3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    ],
    [
        [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9],
        [14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6],
        [4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14],
        [11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    ],
    [
        [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11],
        [10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8],
        [9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6],
        [4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    ],
    [
        [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1],
        [13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6],
        [1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2],
        [6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    ],
    [
        [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7],
        [1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2],
        [7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8],
        [2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11],
    ],
]


def md5_hex(s):
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.md5(s).hexdigest()


def md5_upper(s):
    return md5_hex(s).upper()


def b64_no_wrap(data):
    return base64.b64encode(data).decode("ascii")


def android_quote(s):
    return quote_plus(s, safe="")


def form_encode_items(items):
    return "&".join(f"{k}={android_quote(str(v))}" for k, v in items)


def cuid_from_android_id(android_id):
    return md5_upper("com.baidu" + (android_id or "")) + "|0"


def adid_checksum(md5_text):
    if not re.fullmatch(r"[0-9a-f]{32}", md5_text or ""):
        return "00000000"
    high64 = int(md5_text[:16], 16)
    low64 = int(md5_text[16:], 16)
    folded = high64 ^ low64
    high32 = (folded >> 32) & 0xFFFFFFFF
    low32 = folded & 0xFFFFFFFF
    return f"{(high32 ^ low32) & 0xFFFFFFFF:08x}"


def adid_from_android_id(android_id):
    seed = "alpha.beta" + (android_id or "")
    prefix = md5_hex(seed)
    return prefix + adid_checksum(prefix)


def bits_from_bytes_lsb(data):
    out = []
    for b in data:
        out.extend((b >> i) & 1 for i in range(8))
    return out


def bytes_from_bits_lsb(bits):
    out = bytearray()
    for i in range(0, len(bits), 8):
        b = 0
        for bit in range(8):
            b |= (bits[i + bit] & 1) << bit
        out.append(b)
    return bytes(out)


def permute(bits, table):
    return [bits[i] for i in table]


def rotate_left(values, n):
    return values[n:] + values[:n]


def native_des_subkeys(key):
    key_bytes = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    if len(key_bytes) != 8:
        raise ValueError(f"native DES key must be 8 bytes, got {len(key_bytes)}")
    bits = permute(bits_from_bytes_lsb(key_bytes), PC1)
    left, right = bits[:28], bits[28:]
    subkeys = []
    for shift in SHIFTS:
        left = rotate_left(left, shift)
        right = rotate_left(right, shift)
        subkeys.append(permute(left + right, PC2))
    return subkeys


def native_des_f(right, subkey):
    expanded = permute(right, E)
    mixed = [a ^ b for a, b in zip(expanded, subkey, strict=False)]
    s_bits = []
    for box_index in range(8):
        block = mixed[box_index * 6:(box_index + 1) * 6]
        row = block[0] * 2 + block[5]
        col = block[1] * 8 + block[2] * 4 + block[3] * 2 + block[4]
        value = SBOX[box_index][row][col]
        s_bits.extend([(value >> 3) & 1, (value >> 2) & 1, (value >> 1) & 1, value & 1])
    return permute(s_bits, P)


def native_des_block(block, subkeys):
    bits = permute(bits_from_bytes_lsb(block), IP)
    left, right = bits[:32], bits[32:]
    for subkey in subkeys:
        new_right = [a ^ b for a, b in zip(left, native_des_f(right, subkey), strict=False)]
        left, right = right, new_right
    return bytes_from_bits_lsb(permute(right + left, FP))


def native_des_encrypt(plain, key):
    plain = plain.encode("utf-8") if isinstance(plain, str) else bytes(plain)
    subkeys = native_des_subkeys(key)
    padded_len = (len(plain) & ~7) + 8
    pad_len = padded_len - len(plain)
    padded = bytearray(plain + b"\x00" * pad_len)
    padded[(len(plain) & ~7) + 7] = pad_len
    return b"".join(native_des_block(bytes(padded[i:i + 8]), subkeys) for i in range(0, padded_len, 8))


def native_des_decrypt(cipher, key):
    cipher = bytes(cipher)
    if len(cipher) % 8:
        raise ValueError("native DES ciphertext length must be a multiple of 8")
    subkeys = list(reversed(native_des_subkeys(key)))
    plain = b"".join(native_des_block(cipher[i:i + 8], subkeys) for i in range(0, len(cipher), 8))
    if not plain:
        return plain
    pad_len = plain[-1]
    if pad_len > len(plain):
        raise ValueError(f"invalid native DES padding length: {pad_len}")
    return plain[:-pad_len]


def rev4(value):
    return ((value & 1) << 3) | ((value & 2) << 1) | ((value & 4) >> 1) | ((value & 8) >> 3)


def native_hex_encode(data, add_newline=False):
    parts = []
    for b in data:
        parts.append(f"{rev4(b & 0x0F):02x}{rev4((b >> 4) & 0x0F):02x}")
    text = "".join(parts)
    return text + ("\n" if add_newline else "")


def native_hex_decode(text):
    clean = text[: (len(text) // 4) * 4]
    out = bytearray()
    for i in range(0, len(clean), 4):
        low = rev4(int(clean[i + 1], 16))
        high = rev4(int(clean[i + 3], 16))
        out.append(low | (high << 4))
    return bytes(out)


def generate_rand10():
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def token_from_sign_b(sign_b, rand10):
    key = rand10[:5] + "#G4"
    plain = native_des_decrypt(native_hex_decode(sign_b), key)
    if len(plain) < 22:
        raise ValueError(f"unexpected signB plaintext length {len(plain)}: {plain!r}")
    if plain[:10].decode("latin1") != rand10:
        raise ValueError(f"signB rand mismatch: {plain[:10]!r} != {rand10!r}")
    return plain[12:22].decode("latin1"), plain


def native_get_key(type_str, token):
    m0 = md5_hex(KEY_SALT)
    m1 = md5_hex(type_str)
    m2 = md5_hex(f"[{token}]@")
    m2 = m2[17:32][::-1] + m2[15:17] + m2[0:15][::-1]
    chars = list(m0 + m1 + m2)
    for i in range(3):
        chars[i], chars[-1 - i] = chars[-1 - i], chars[i]
    s = "".join(chars)
    out = s + md5_hex(s)
    chars = list(out)
    left, right = 0, len(chars) - 1
    for _ in range(60):
        chars[left], chars[right] = chars[right], chars[left]
        left += 1
        right -= 1
    return "".join(chars)


def native_get_sign(base64_param_string, token):
    return md5_hex(f"{MAGIC}[{md5_hex(token)}]@{base64_param_string}")


def rc4_crypt(data, key):
    key_bytes = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key_bytes[i % len(key_bytes)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for b in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def build_antispam_request(sign_a, common_params):
    body_items = [("data", sign_a)] + common_params
    return {
        "body": form_encode_items(body_items) + "&",
        "params_for_body": body_items,
    }


def extract_sign_b(payload):
    """Pull signB out of an antispam response payload, or return "" if absent.

    Depending on the gateway it sits at data, data.data or result.data.
    """
    candidate = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(candidate, dict):
        candidate = candidate.get("data")
    if not isinstance(candidate, str):
        result_obj = payload.get("result") if isinstance(payload, dict) else None
        candidate = result_obj.get("data") if isinstance(result_obj, dict) else None
    return candidate if isinstance(candidate, str) and candidate else ""


def build_share_request(code, token, version_code, common_params, nt="wifi", did="", server_time=None, kakorr=None):
    type_str = str(version_code)
    rc4_key = native_get_key(type_str, token)
    plain_params = "key=" + android_quote(code)
    data_value = b64_no_wrap(rc4_crypt(plain_params.encode("utf-8"), rc4_key))
    server_time = int(server_time if server_time is not None else time.time())
    kakorr = int(kakorr if kakorr is not None else time.monotonic() * 1000)

    sign_list = [f"data={data_value}"]
    sign_list.extend(f"{k}={v}" for k, v in common_params)
    if did:
        sign_list.append(f"did={did}")
    sign_list.extend([
        f"nt={nt}",
        f"_t_={server_time}",
        f"kakorrhaphiophobia={kakorr}",
    ])
    joined = "".join(sorted(sign_list))
    base64_param = b64_no_wrap(joined.encode("utf-8"))
    sign = native_get_sign(base64_param, token)
    sign_pack = f"{sign}&_t_={server_time}&kakorrhaphiophobia={kakorr}"

    # HttpCurrencyRequest.toString() emits "?&data=...", so the real POST body
    # keeps a leading '&' after df.d strips the URL path.
    extra_params = [("did", did)] if did else []
    body = "&" + form_encode_items([("data", data_value)] + common_params + extra_params + [("nt", nt)])
    body += f"&sign={sign_pack}"
    return {
        "plain_params": plain_params,
        "rc4_key": rc4_key,
        "data": data_value,
        "sign_list": sorted(sign_list),
        "joined_sign_params": joined,
        "base64_sign_params": base64_param,
        "sign": sign,
        "body": body,
    }
