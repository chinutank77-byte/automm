# ==========================================
# send.py
# Crazy MM & Exchange — Litecoin Sender (Ultra)
# Supports sending TO:
#   - P2PKH   (L...)      Legacy
#   - P2SH    (M...)      Nested SegWit / multisig
#   - P2WPKH  (ltc1q...20B)
#   - P2WSH   (ltc1q...32B)
#   - P2TR    (ltc1p...)  Taproot (Bech32m)
#   - MWEB    (ltc1mweb...) → EXPLICITLY REJECTED
#
# Escrow FROM: P2PKH (Legacy) only — signing is P2PKH
# Fee: deducted from total UTXO (deal amount minus fee)
# No external bip_utils/bitcoinlib — pure ecdsa/base58/hashlib/struct/requests
# ==========================================
import os
import hashlib
import struct
import logging
import requests
import ecdsa
import base58
from typing import Optional, List, Dict, Tuple
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("Send")

# ==========================================
# CONFIGURATION
# ==========================================
SATOSHIS_PER_LTC = 100_000_000
DUST_LIMIT = 546

DEFAULT_FEE_SATS = 10_000        # 0.0001 LTC

# LTC mainnet version bytes
LTC_P2PKH_VERSION = 0x30         # "L..." prefix
LTC_P2SH_VERSION = 0x32          # "M..." prefix (LTC specific)
LTC_P2SH_COMPAT = 0x05           # BTC-compat P2SH
LTC_WIF_VERSION = 0xB0

# Bech32 / Bech32m
BECH32_HRP_LTC = "ltc"
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_CHARSET_REV = {c: i for i, c in enumerate(BECH32_CHARSET)}
BECH32M_CONST = 0x2bc830a3

# APIs
BLOCKCYPHER_BASE = "https://api.blockcypher.com/v1/ltc/main"
BLOCKCYPHER_TOKEN = os.getenv("BLOCKCYPHER_TOKEN", "").strip()
EXPLORER_TX_BASE = "https://live.blockcypher.com/ltc/tx"


# ==========================================
# HASH HELPERS
# ==========================================
def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _ripemd160(data: bytes) -> bytes:
    return hashlib.new("ripemd160", data).digest()


def _hash160(data: bytes) -> bytes:
    return _ripemd160(_sha256(data))


# ==========================================
# BASE58CHECK
# ==========================================
def _base58check_encode(payload: bytes) -> str:
    checksum = _double_sha256(payload)[:4]
    return base58.b58encode(payload + checksum).decode()


def _base58check_decode(s: str) -> bytes:
    raw = base58.b58decode(s)
    if len(raw) < 5:
        raise ValueError("Base58 payload too short")
    payload, checksum = raw[:-4], raw[-4:]
    if _double_sha256(payload)[:4] != checksum:
        raise ValueError("Invalid Base58Check checksum")
    return payload


# ==========================================
# BECH32 / BECH32M DECODER (internal)
# ==========================================
def _bech32_polymod(values: List[int]) -> int:
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> List[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_verify_checksum(hrp: str, data: List[int]) -> Optional[str]:
    const = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if const == 1:
        return "bech32"
    if const == BECH32M_CONST:
        return "bech32m"
    return None


def _bech32_decode(bech: str) -> Tuple[Optional[str], Optional[List[int]], Optional[str]]:
    """Returns (hrp, data_without_checksum, spec) or (None, None, None)."""
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return (None, None, None)
    if bech.lower() != bech and bech.upper() != bech:
        return (None, None, None)
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech) or len(bech) > 90:
        return (None, None, None)
    if not all(x in BECH32_CHARSET for x in bech[pos + 1:]):
        return (None, None, None)
    hrp = bech[:pos]
    data = [BECH32_CHARSET_REV[x] for x in bech[pos + 1:]]
    spec = _bech32_verify_checksum(hrp, data)
    if spec is None:
        return (None, None, None)
    return (hrp, data[:-6], spec)


def _convertbits(data: List[int], frombits: int, tobits: int, pad: bool = True) -> Optional[List[int]]:
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


# ==========================================
# ADDRESS PARSER (multi-type)
# ==========================================
def parse_address(address: str) -> Dict:
    """
    Returns:
      {
        "type": "p2pkh"|"p2sh"|"p2wpkh"|"p2wsh"|"p2tr",
        "hash": bytes,
        "script": bytes,      # scriptPubKey
        "address": original,
      }
    Raises ValueError for MWEB / testnet / unsupported.
    """
    if not address or not isinstance(address, str):
        raise ValueError("Address must be a non-empty string")

    address = address.strip()

    # Reject obvious MWEB
    if "mweb" in address.lower():
        raise ValueError("MWEB addresses are not supported by this sender")

    # Reject testnet
    if address.startswith(("tltc1",)):
        raise ValueError(f"Testnet address not supported: {address}")

    lower = address.lower()

    # Bech32 / Bech32m
    if lower.startswith("ltc1"):
        return _parse_bech32(address)

    # Base58Check
    return _parse_base58(address)


def _parse_bech32(address: str) -> Dict:
    hrp, data, spec = _bech32_decode(address)
    if hrp is None:
        raise ValueError(f"Invalid Bech32/Bech32m address: {address}")
    if hrp != BECH32_HRP_LTC:
        raise ValueError(f"Unexpected HRP: {hrp} (expected 'ltc')")
    if not data:
        raise ValueError("Empty bech32 data")

    witness_version = data[0]
    prog = _convertbits(data[1:], 5, 8, False)
    if prog is None:
        raise ValueError("Invalid bech32 data padding")
    prog = bytes(prog)

    if witness_version == 0 and spec == "bech32":
        if len(prog) == 20:
            return {
                "type": "p2wpkh",
                "hash": prog,
                "script": b"\x00\x14" + prog,   # OP_0 PUSH20 <20>
                "address": address,
            }
        elif len(prog) == 32:
            return {
                "type": "p2wsh",
                "hash": prog,
                "script": b"\x00\x20" + prog,   # OP_0 PUSH32 <32>
                "address": address,
            }
        else:
            raise ValueError(f"Invalid witness-v0 program length: {len(prog)}")

    if witness_version == 1 and spec == "bech32m":
        if len(prog) == 32:
            return {
                "type": "p2tr",
                "hash": prog,
                "script": b"\x51\x20" + prog,   # OP_1 PUSH32 <32>
                "address": address,
            }
        else:
            raise ValueError(f"Invalid taproot program length: {len(prog)}")

    raise ValueError(
        f"Unsupported witness version {witness_version} ({spec}) "
        f"or invalid program length"
    )


def _parse_base58(address: str) -> Dict:
    payload = _base58check_decode(address)
    if len(payload) != 21:
        raise ValueError(f"Invalid Base58Check payload length: {len(payload)}")

    version = payload[0]
    h160 = payload[1:21]

    if version == LTC_P2PKH_VERSION:
        return {
            "type": "p2pkh",
            "hash": h160,
            "script": b"\x76\xa9\x14" + h160 + b"\x88\xac",
            "address": address,
        }

    if version in (LTC_P2SH_VERSION, LTC_P2SH_COMPAT):
        return {
            "type": "p2sh",
            "hash": h160,
            "script": b"\xa9\x14" + h160 + b"\x87",
            "address": address,
        }

    raise ValueError(f"Unsupported Base58 version byte: {hex(version)}")


def address_to_script_pubkey(address: str) -> bytes:
    """Backwards-compatible wrapper."""
    return parse_address(address)["script"]


# ==========================================
# ADDRESS VALIDATION HELPER (Discord-friendly)
# ==========================================
def validate_recipient_address(address: str) -> Tuple[bool, str]:
    """
    Validate LTC recipient address.
    Returns: (is_valid, error_message)
    error_message empty string if valid.
    """
    if not address or not address.strip():
        return False, "❌ No address provided."

    address = address.strip()
    lower = address.lower()

    # ---- MWEB (explicit, earliest check) ----
    if lower.startswith("ltc1mweb") or "mweb" in lower:
        return False, (
            "❌ **MWEB addresses are not supported** by our escrow system.\n"
            "Please provide a **Legacy (L...)**, **P2SH (M...)**, "
            "**Native SegWit (ltc1q...)**, or **Taproot (ltc1p...)** address."
        )

    # ---- Testnet ----
    if lower.startswith("tltc1"):
        return False, (
            "❌ **Testnet addresses are not supported.**\n"
            "Please provide a Litecoin **mainnet** address."
        )

    if address.startswith(("m", "n", "Q", "2")) and len(address) == 34:
        # 'm'/'n' testnet P2PKH, 'Q' testnet P2SH, '2' BTC testnet P2SH
        # Note: uppercase 'M' is mainnet P2SH → skip
        return False, (
            "❌ **Testnet addresses are not supported.**\n"
            "Please provide a Litecoin **mainnet** address."
        )

    # ---- Parse via parse_address ----
    try:
        info = parse_address(address)
    except ValueError as e:
        err_str = str(e)
        if "MWEB" in err_str:
            return False, (
                "❌ **MWEB addresses are not supported.**\n"
                "Please use a different address type."
            )
        if "Testnet" in err_str:
            return False, (
                "❌ **Testnet addresses are not supported.**\n"
                "Please use a mainnet address."
            )
        return False, f"❌ Invalid address: {err_str}"

    # ---- Supported types check ----
    supported = ("p2pkh", "p2sh", "p2wpkh", "p2wsh", "p2tr")
    if info["type"] not in supported:
        return False, f"❌ Unsupported address type: `{info['type']}`"

    return True, ""


# ==========================================
# PRIVATE KEY <-> WIF
# ==========================================
def hex_priv_to_wif(priv_hex: str, compressed: bool = True) -> str:
    priv_bytes = bytes.fromhex(priv_hex)
    if len(priv_bytes) != 32:
        raise ValueError("Private key must be 32 bytes")
    payload = bytes([LTC_WIF_VERSION]) + priv_bytes
    if compressed:
        payload += b"\x01"
    return _base58check_encode(payload)


def wif_to_hex_priv(wif: str) -> Tuple[str, bool]:
    payload = _base58check_decode(wif)
    if len(payload) == 34:
        return payload[1:33].hex(), True
    if len(payload) == 33:
        return payload[1:33].hex(), False
    raise ValueError("Invalid WIF length")


# ==========================================
# PUBLIC KEY
# ==========================================
def _priv_to_pubkey(priv_hex: str, compressed: bool = True) -> bytes:
    priv_bytes = bytes.fromhex(priv_hex)
    signing_key = ecdsa.SigningKey.from_string(priv_bytes, curve=ecdsa.SECP256k1)
    vk = signing_key.get_verifying_key()
    x = vk.pubkey.point.x()
    y = vk.pubkey.point.y()
    if compressed:
        prefix = b"\x02" if (y % 2 == 0) else b"\x03"
        return prefix + x.to_bytes(32, "big")
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def pubkey_to_p2pkh_address(pubkey: bytes) -> str:
    h160 = _hash160(pubkey)
    return _base58check_encode(bytes([LTC_P2PKH_VERSION]) + h160)


# ==========================================
# VARINT
# ==========================================
def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
    first = data[pos]
    if first < 0xfd:
        return first, 1
    if first == 0xfd:
        return struct.unpack("<H", data[pos + 1:pos + 3])[0], 3
    if first == 0xfe:
        return struct.unpack("<I", data[pos + 1:pos + 5])[0], 5
    return struct.unpack("<Q", data[pos + 1:pos + 9])[0], 9


def _write_varint(n: int) -> bytes:
    if n < 0xfd:
        return struct.pack("<B", n)
    if n <= 0xffff:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


# ==========================================
# UTXO FETCH
# ==========================================
def fetch_utxos(address: str) -> List[Dict]:
    """Fetch unspent P2PKH outputs via BlockCypher."""
    url = f"{BLOCKCYPHER_BASE}/addrs/{address}"
    params = {"unspentOnly": "true", "includeScript": "true"}
    if BLOCKCYPHER_TOKEN:
        params["token"] = BLOCKCYPHER_TOKEN

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"UTXO fetch failed for {address}: {e}")
        return []

    data = resp.json()
    utxos = []
    for ref in data.get("txrefs", []):
        if ref.get("tx_output_n") is None:
            continue
        utxos.append({
            "txid": ref["tx_hash"],
            "vout": int(ref["tx_output_n"]),
            "value": int(ref["value"]),
            "script": ref.get("script"),
        })

    logger.info(f"📍 Fetched {len(utxos)} UTXOs for {address}")
    return utxos


# ==========================================
# UNSIGNED TX
# ==========================================
def _build_unsigned_tx(
    utxos: List[Dict],
    to_script: bytes,
    send_sats: int,
    change_script: Optional[bytes] = None,
    change_sats: int = 0,
) -> bytes:
    version = struct.pack("<I", 2)

    inputs_data = b""
    for u in utxos:
        prev_hash = bytes.fromhex(u["txid"])[::-1]
        prev_vout = struct.pack("<I", u["vout"])
        inputs_data += prev_hash + prev_vout + _write_varint(0) + struct.pack("<I", 0xffffffff)

    outputs_data = (
        struct.pack("<Q", send_sats)
        + _write_varint(len(to_script))
        + to_script
    )

    n_outputs = 1
    if change_script is not None and change_sats > 0:
        outputs_data += (
            struct.pack("<Q", change_sats)
            + _write_varint(len(change_script))
            + change_script
        )
        n_outputs = 2

    locktime = struct.pack("<I", 0)

    return (
        version
        + _write_varint(len(utxos))
        + inputs_data
        + _write_varint(n_outputs)
        + outputs_data
        + locktime
    )


def _parse_unsigned_tx(tx: bytes):
    pos = 0
    version = tx[pos:pos + 4]; pos += 4

    ic, c = _read_varint(tx, pos); pos += c
    inputs = []
    for _ in range(ic):
        prev_hash = tx[pos:pos + 32]; pos += 32
        prev_vout = tx[pos:pos + 4]; pos += 4
        sl, c = _read_varint(tx, pos); pos += c
        script = tx[pos:pos + sl]; pos += sl
        seq = tx[pos:pos + 4]; pos += 4
        inputs.append({"prev_hash": prev_hash, "prev_vout": prev_vout, "script": script, "seq": seq})

    oc, c = _read_varint(tx, pos); pos += c
    outputs = []
    for _ in range(oc):
        value = tx[pos:pos + 8]; pos += 8
        sl, c = _read_varint(tx, pos); pos += c
        script = tx[pos:pos + sl]; pos += sl
        outputs.append({"value": value, "script": script})

    locktime = tx[pos:pos + 4]
    return version, inputs, outputs, locktime


def _serialize_tx(version, inputs, outputs, locktime) -> bytes:
    data = version + _write_varint(len(inputs))
    for inp in inputs:
        data += inp["prev_hash"] + inp["prev_vout"]
        data += _write_varint(len(inp["script"])) + inp["script"]
        data += inp["seq"]
    data += _write_varint(len(outputs))
    for out in outputs:
        data += out["value"]
        data += _write_varint(len(out["script"])) + out["script"]
    data += locktime
    return data


# ==========================================
# SIGNING (P2PKH escrow)
# ==========================================
def _sign_input(unsigned_tx: bytes, input_index: int, priv_hex: str,
                our_pubkey: bytes, our_script_pubkey: bytes) -> bytes:
    version, inputs, outputs, locktime = _parse_unsigned_tx(unsigned_tx)

    temp_inputs = []
    for i, inp in enumerate(inputs):
        if i == input_index:
            temp_inputs.append({
                "prev_hash": inp["prev_hash"],
                "prev_vout": inp["prev_vout"],
                "script": our_script_pubkey,
                "seq": inp["seq"],
            })
        else:
            temp_inputs.append({
                "prev_hash": inp["prev_hash"],
                "prev_vout": inp["prev_vout"],
                "script": b"",
                "seq": inp["seq"],
            })

    preimage = _serialize_tx(version, temp_inputs, outputs, locktime)
    preimage += struct.pack("<I", 1)  # SIGHASH_ALL

    sighash = _double_sha256(preimage)

    priv_bytes = bytes.fromhex(priv_hex)
    signing_key = ecdsa.SigningKey.from_string(priv_bytes, curve=ecdsa.SECP256k1)
    sig = signing_key.sign_digest_deterministic(
        sighash, hashfunc=hashlib.sha256, sigencode=ecdsa.util.sigencode_der,
    )
    sig_with_type = sig + b"\x01"

    return (
        _write_varint(len(sig_with_type)) + sig_with_type
        + _write_varint(len(our_pubkey)) + our_pubkey
    )


def _sign_all_inputs(unsigned_tx: bytes, utxos: List[Dict], priv_hex: str) -> bytes:
    pubkey = _priv_to_pubkey(priv_hex, compressed=True)
    our_address = pubkey_to_p2pkh_address(pubkey)
    our_script = parse_address(our_address)["script"]

    version, inputs, outputs, locktime = _parse_unsigned_tx(unsigned_tx)

    for i in range(len(inputs)):
        inputs[i]["script"] = _sign_input(unsigned_tx, i, priv_hex, pubkey, our_script)

    return _serialize_tx(version, inputs, outputs, locktime)


# ==========================================
# BROADCAST
# ==========================================
def broadcast_tx(signed_hex: str) -> Optional[str]:
    url = f"{BLOCKCYPHER_BASE}/txs/push"
    if BLOCKCYPHER_TOKEN:
        url += f"?token={BLOCKCYPHER_TOKEN}"

    try:
        resp = requests.post(url, json={"tx": signed_hex}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        txid = data.get("tx", {}).get("hash") or data.get("hash")
        if txid:
            logger.info(f"✅ Broadcast success: {txid}")
            return txid
        logger.error(f"Broadcast missing txid: {data}")
        return None
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        logger.error(f"Broadcast HTTP error: {e} | body: {body[:300]}")
        return None
    except Exception as e:
        logger.error(f"Broadcast error: {e}")
        return None


# ==========================================
# HIGH-LEVEL SEND
# ==========================================
def send_ltc(
    from_priv_hex: str,
    to_address: str,
    amount_ltc: Optional[float] = None,
    fee_sats: int = DEFAULT_FEE_SATS,
) -> Dict:
    """
    Send LTC from escrow (P2PKH) → any supported address type.

    Amount logic:
      - If `amount_ltc` is provided AND the address holds at least
        `amount_ltc + fee` (overpayment), send exactly `amount_ltc` to the
        recipient, pay the fee out of the surplus, and return the remainder
        to the escrow address as change.
      - Otherwise (exact or short payment), send the full balance minus fee.

    Returns:
      {
        "success": bool,
        "txid": str|None,
        "explorer_url": str|None,
        "from_address": str|None,
        "to_address": str,
        "to_type": str|None,
        "send_sats": int,
        "fee_sats": int,
        "error": str|None,
      }
    """
    result = {
        "success": False,
        "txid": None,
        "explorer_url": None,
        "from_address": None,
        "to_address": to_address,
        "to_type": None,
        "send_sats": 0,
        "fee_sats": fee_sats,
        "change_sats": 0,
        "error": None,
    }

    # 0) Recipient validation (MWEB / testnet reject)
    ok, err_msg = validate_recipient_address(to_address)
    if not ok:
        result["error"] = err_msg
        logger.error(f"Recipient validation failed: {err_msg}")
        return result

    # 1) Parse recipient (should succeed now)
    try:
        recipient = parse_address(to_address)
        result["to_type"] = recipient["type"]
    except Exception as e:
        result["error"] = f"Invalid recipient address: {e}"
        logger.error(result["error"])
        return result

    # 2) Derive escrow address
    try:
        pubkey = _priv_to_pubkey(from_priv_hex, compressed=True)
        from_address = pubkey_to_p2pkh_address(pubkey)
        result["from_address"] = from_address
    except Exception as e:
        result["error"] = f"Address derivation failed: {e}"
        logger.error(result["error"])
        return result

    logger.info(f"💸 Preparing send {from_address} → {to_address} ({recipient['type']})")

    # 3) Fetch UTXOs
    utxos = fetch_utxos(from_address)
    if not utxos:
        result["error"] = "No UTXOs found (no funds / API failed)"
        logger.error(result["error"])
        return result

    total_sats = sum(u["value"] for u in utxos)
    logger.info(f"💰 UTXO total: {total_sats} sats ({total_sats / SATOSHIS_PER_LTC:.8f} LTC)")

    # 4) Amount + fee logic
    #    expected_sats: deal amount (0 when not provided -> legacy sweep behaviour)
    expected_sats = int(round(amount_ltc * SATOSHIS_PER_LTC)) if amount_ltc else 0

    if expected_sats > 0 and total_sats >= expected_sats + fee_sats:
        # Overpaid (enough to cover deal amount + fee): send exact deal amount,
        # fee comes out of the surplus, remainder goes back as change.
        send_sats = expected_sats
        change_sats = total_sats - expected_sats - fee_sats
    else:
        # Exact / short payment: send everything minus the network fee.
        send_sats = total_sats - fee_sats
        change_sats = 0

    if send_sats <= DUST_LIMIT:
        result["error"] = (
            f"Insufficient after fee: total={total_sats}, fee={fee_sats}, "
            f"remaining={send_sats}"
        )
        logger.error(result["error"])
        return result

    # Dust change (below relay threshold) -> fold into miner fee (skip output).
    if 0 < change_sats < DUST_LIMIT:
        logger.info(f"🧹 Change below dust ({change_sats} sats) folded into fee.")
        change_sats = 0

    change_script = None
    if change_sats > 0:
        try:
            change_script = parse_address(from_address)["script"]
        except Exception:
            logger.warning("Change address parse failed — skipping change output.")
            change_script = None
            change_sats = 0

    result["send_sats"] = send_sats
    result["change_sats"] = change_sats
    logger.info(
        f"🧾 Send: {send_sats} sats ({send_sats / SATOSHIS_PER_LTC:.8f} LTC) | "
        f"Fee: {fee_sats} sats | Change: {change_sats} sats"
    )

    # 5) Build unsigned
    try:
        unsigned_tx = _build_unsigned_tx(
            utxos, recipient["script"], send_sats, change_script, change_sats
        )
    except Exception as e:
        result["error"] = f"TX build failed: {e}"
        logger.error(result["error"], exc_info=True)
        return result

    # 6) Sign
    try:
        signed_tx = _sign_all_inputs(unsigned_tx, utxos, from_priv_hex)
        signed_hex = signed_tx.hex()
    except Exception as e:
        result["error"] = f"Signing failed: {e}"
        logger.error(result["error"], exc_info=True)
        return result

    # 7) Broadcast
    txid = broadcast_tx(signed_hex)
    if not txid:
        result["error"] = "Broadcast failed"
        return result

    result["success"] = True
    result["txid"] = txid
    result["explorer_url"] = f"{EXPLORER_TX_BASE}/{txid}"

    logger.info(f"✅ Sent {send_sats / SATOSHIS_PER_LTC:.8f} LTC | TX: {txid}")
    return result


# ==========================================
# TEST (direct run only)
# ==========================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("🔎 Address Validation Tests")
    print("=" * 60)

    test_addresses = [
        ("LdP8Qox1VAhCzLJNqrr74YovaWYyNBUWvL", "P2PKH Legacy"),
        ("ltc1qhq9qalsf6pvk5x7vmyk9l8dcngnw7rlrqrkqpv", "P2WPKH Native SegWit"),
        ("M8T1B2Z97gVdvmfkQcAtYbEepune1tzGua", "P2SH"),
        ("ltc1mweb1qxxxxxxxxxxxxxxxxxxxxxxxxxx", "MWEB (should reject)"),
        ("tltc1qtestnetxxxxxxxxxxxxxxxxxxxxx", "Testnet (should reject)"),
        ("invalid_xyz_123", "Invalid (should reject)"),
        ("", "Empty (should reject)"),
    ]

    for addr, label in test_addresses:
        ok, msg = validate_recipient_address(addr)
        status = "✅ VALID" if ok else "❌ INVALID"
        display = addr if len(addr) <= 40 else addr[:37] + "..."
        print(f"{status} | {label}")
        print(f"       Address: {display}")
        if not ok:
            print(f"       Reason:  {msg.splitlines()[0]}")
        print()

    print("=" * 60)
    print("✅ send.py loaded successfully.")
    print("=" * 60)