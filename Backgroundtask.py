import discord
from discord.ext import tasks
import aiohttp
import asyncio
import logging
import os
import sys
import time
import json
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv
from DealConfirmation import send_deal_confirmation
from ui.PaymentDetected import send_payment_detected

load_dotenv()

os.makedirs("logs", exist_ok=True)

logger = logging.getLogger("BackgroundTask")
logger.setLevel(logging.DEBUG)
logger.propagate = False

if not logger.handlers:
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console_fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | [%(name)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    console.setFormatter(console_fmt)
    logger.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        "logs/backgroundtask.log",
        when="midnight",
        interval=1,
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)


BLOCKCYPHER_TOKEN = os.getenv("BLOCKCYPHER_TOKEN", "").strip()
SOCHAIN_API_KEY = os.getenv("SOCHAIN_API_KEY", "").strip()

POLL_INTERVAL_SECONDS = 22
TRANSCRIPT_INTERVAL_SECONDS = 15
CHANNEL_DELETE_CHECK_INTERVAL = 30
TASK_QUEUE_INTERVAL = 20

REQUIRED_CONFIRMATIONS = 1
RETRY_BASE_DELAY = 3
MAX_BACKOFF_DELAY = 60
REQUEST_TIMEOUT = 15
MAX_RATE_LIMIT_WAIT = 12
MIN_RATE_LIMIT_WAIT = 30
SATOSHIS_PER_LTC = 100_000_000

BLOCKCYPHER_BASE = "https://api.blockcypher.com/v1/ltc/main"
SOCHAIN_BASE = "https://chain.so/api/v3"
LITESPACE_BASE = "https://litecoinspace.org"
BLOCKCHAIR_BASE = "https://api.blockchair.com"
BLOCKBOOK_BASE = "https://ltc1.trezor.io"

EXACT_MATCH_RATIO = 0.99
OVERPAY_NOTICE_RATIO = 1.05
LOW_AMOUNT_USD_THRESHOLD = 0.03

BASE_DIR = Path(__file__).resolve().parent
DETAIL_BACKUP_DIR = BASE_DIR / "detail_backup"

_bot: discord.Client = None


def start_payment_checker(bot: discord.Client):
    global _bot
    _bot = bot

    if not BLOCKCYPHER_TOKEN:
        logger.warning(
            "BLOCKCYPHER_TOKEN not set — free tier (200 req/h) may rate-limit."
        )

    if not payment_checker_loop.is_running():
        payment_checker_loop.start(bot)
        logger.info("Payment Checker started (PARALLEL: BlockCypher + SoChain v3).")
        logger.debug(f"Config: POLL={POLL_INTERVAL_SECONDS}s, CONFIRMATIONS={REQUIRED_CONFIRMATIONS}")
    else:
        logger.warning("Payment Checker already running.")

    if not transcript_loop.is_running():
        try:
            from backup_manager.transcript_manager import process_active_transcripts
            transcript_loop.start(bot, process_active_transcripts)
            logger.info(f"Transcript loop started (every {TRANSCRIPT_INTERVAL_SECONDS}s).")
        except ImportError as e:
            logger.warning(f"Transcript loop not started — module missing: {e}")
        except Exception as e:
            logger.error(f"Transcript loop start failed: {e}")

    if not channel_cleanup_loop.is_running():
        channel_cleanup_loop.start(bot)
        logger.info(f"Channel cleanup loop started (every {CHANNEL_DELETE_CHECK_INTERVAL}s).")

    if not task_queue_loop.is_running():
        task_queue_loop.start(bot)
        logger.info(f"Task queue loop started (every {TASK_QUEUE_INTERVAL}s).")


def _retry_delay(attempt: int) -> float:
    return min(RETRY_BASE_DELAY * (2 ** attempt), MAX_BACKOFF_DELAY)


async def http_get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict = None,
    headers: dict = None,
    timeout: int = None,
    label: str = "API",
    max_attempts: int = 3,
):
    if params is None:
        params = {}
    if headers is None:
        headers = {}
    if timeout is None:
        timeout = REQUEST_TIMEOUT

    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        try:
            logger.debug(f"[{label}] GET {url[:80]} (attempt {attempt}/{max_attempts})")
            async with session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.debug(f"[{label}] Success ({resp.status})")
                    return data

                if resp.status == 429:
                    try:
                        retry_after = int(resp.headers.get("Retry-After", 0))
                    except (TypeError, ValueError):
                        retry_after = 0

                    if retry_after <= 0:
                        retry_after = MIN_RATE_LIMIT_WAIT
                    retry_after = min(retry_after, MAX_RATE_LIMIT_WAIT if MAX_RATE_LIMIT_WAIT >= MIN_RATE_LIMIT_WAIT else MIN_RATE_LIMIT_WAIT)

                    logger.warning(
                        f"[{label}] Rate limited — abandoning cycle "
                        f"(would wait {retry_after}s)."
                    )
                    return None

                if resp.status >= 500:
                    logger.debug(f"[{label}] Server error {resp.status}, retrying...")
                    await asyncio.sleep(_retry_delay(attempt))
                    continue

                body = await resp.text()
                logger.error(f"[{label}] HTTP {resp.status}: {body[:200]}")
                return None

        except asyncio.TimeoutError:
            logger.debug(f"[{label}] Timeout (attempt {attempt})")
            if attempt < max_attempts:
                await asyncio.sleep(_retry_delay(attempt))
        except aiohttp.ClientError as e:
            logger.debug(f"[{label}] Network error: {e}")
            if attempt < max_attempts:
                await asyncio.sleep(_retry_delay(attempt))
        except Exception as e:
            logger.error(f"[{label}] Unexpected: {e}")
            if attempt < max_attempts:
                await asyncio.sleep(_retry_delay(attempt))

    logger.debug(f"[{label}] All {max_attempts} attempts exhausted.")
    return None


async def _fetch_blockcypher(session: aiohttp.ClientSession, address: str):
    logger.debug(f"[BlockCypher] Fetching {address[:20]}...")
    url = f"{BLOCKCYPHER_BASE}/addrs/{address}"
    params = {}
    if BLOCKCYPHER_TOKEN:
        params["token"] = BLOCKCYPHER_TOKEN

    data = await http_get_json(session, url, params=params, label="BlockCypher/addr")
    if not data:
        return None

    def _to_sats(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    balance_sats = _to_sats(data.get("balance"))
    unconfirmed_sats = _to_sats(data.get("unconfirmed_balance"))
    total_sats = max(0, balance_sats + unconfirmed_sats)
    confirmed_sats = max(0, balance_sats)

    txrefs = data.get("txrefs") or []
    unconfirmed_txrefs = data.get("unconfirmed_txrefs") or []

    txid = None
    if unconfirmed_txrefs and unconfirmed_txrefs[0].get("tx_hash"):
        txid = unconfirmed_txrefs[0].get("tx_hash")
    elif txrefs and txrefs[0].get("tx_hash"):
        txid = txrefs[0].get("tx_hash")

    confirmations = 0
    if txrefs:
        confs = []
        for t in txrefs:
            try:
                confs.append(int(t.get("confirmations", 0) or 0))
            except (TypeError, ValueError):
                confs.append(0)
        if confs:
            confirmations = min(confs)

    if total_sats <= 0:
        logger.debug(f"[BlockCypher] {address[:20]} no balance")
        return None

    logger.debug(
        f"[BlockCypher] total={total_sats} sats, confirmed={confirmed_sats} sats, "
        f"conf={confirmations}, tx={txid[:16] if txid else None}"
    )

    return {
        "provider": "blockcypher",
        "txid": txid,
        "amount_ltc": total_sats / SATOSHIS_PER_LTC,
        "confirmed_ltc": confirmed_sats / SATOSHIS_PER_LTC,
        "confirmations": confirmations,
    }


async def _fetch_sochain(session: aiohttp.ClientSession, address: str):
    logger.debug(f"[SoChain] Fetching {address[:20]}...")
    url = f"{SOCHAIN_BASE}/balance/LTC/{address}"
    headers = {}
    if SOCHAIN_API_KEY:
        headers["API-KEY"] = SOCHAIN_API_KEY

    data = await http_get_json(session, url, headers=headers, label="SoChain/balance")
    if not data or data.get("status") != "success":
        return None

    result = data.get("data", {})
    try:
        confirmed = float(result.get("confirmed", 0) or 0)
        unconfirmed = float(result.get("unconfirmed", 0) or 0)
    except (TypeError, ValueError):
        return None

    total = confirmed + unconfirmed
    if total <= 0:
        return None

    txid = None
    tx_url = f"{SOCHAIN_BASE}/transactions/LTC/{address}"
    tx_data = await http_get_json(session, tx_url, headers=headers, label="SoChain/txs")
    if tx_data and tx_data.get("status") == "success":
        txs = tx_data.get("data", {}).get("transactions", [])
        if txs:
            latest_tx = txs[0]
            txid = (
                latest_tx.get("hash")
                or latest_tx.get("txid")
                or latest_tx.get("tx_hash")
            )

    if not txid:
        txid = f"sochain-{address[-12:]}"

    confirmations = 1 if confirmed > 0 else 0

    logger.debug(
        f"[SoChain] confirmed={confirmed} LTC, unconfirmed={unconfirmed} LTC, "
        f"conf~{confirmations}, tx={txid[:16]}"
    )

    return {
        "provider": "sochain",
        "txid": txid,
        "amount_ltc": total,
        "confirmed_ltc": confirmed,
        "confirmations": confirmations,
    }


PROVIDER_NAMES = ["BlockCypher", "SoChain", "LiteSpace", "Blockchair", "Blockbook"]
MIN_PROVIDERS = 2  # 2-of-N consensus required before a payment is considered detected


async def _fetch_blockchair(session: aiohttp.ClientSession, address: str):
    logger.debug(f"[Blockchair] Fetching {address[:20]}...")
    url = f"{BLOCKCHAIR_BASE}/litecoin/dashboards/address/{address}"

    data = await http_get_json(session, url, label="Blockchair/addr")
    if not data:
        return None

    dash = (data.get("data") or {}).get(address)
    if not dash:
        return None

    addr = dash.get("address") or {}

    def _to_sats(value):
        if value is None:
            return 0
        try:
            # integer/string-int -> already in smallest unit (litoshis);
            # float/decimal -> likely in LTC, scale up
            if isinstance(value, float) or (isinstance(value, str) and "." in value):
                return int(round(float(value) * SATOSHIS_PER_LTC))
            return int(value)
        except (TypeError, ValueError):
            return 0

    balance_sats = _to_sats(addr.get("balance"))
    if balance_sats <= 0:
        return None

    txid = None
    txs = dash.get("transactions") or []
    if txs:
        txid = txs[0].get("hash") or txs[0].get("txid")
    if not txid:
        txid = f"blockchair-{address[-12:]}"

    logger.debug(f"[Blockchair] balance={balance_sats} sats, tx={txid[:16]}")

    return {
        "provider": "blockchair",
        "txid": txid,
        "amount_ltc": balance_sats / SATOSHIS_PER_LTC,
        "confirmed_ltc": balance_sats / SATOSHIS_PER_LTC,
        "confirmations": 1,
    }


async def _fetch_blockbook(session: aiohttp.ClientSession, address: str):
    logger.debug(f"[Blockbook] Fetching {address[:20]}...")
    url = f"{BLOCKBOOK_BASE}/api/v2/address/{address}"

    data = await http_get_json(session, url, label="Blockbook/addr")
    if not data:
        return None

    def _to_sats(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    confirmed_sats = _to_sats(data.get("balance"))
    unconfirmed_sats = _to_sats(data.get("unconfirmedBalance"))
    total_sats = confirmed_sats + unconfirmed_sats
    if total_sats <= 0:
        return None

    txid = None
    txids = data.get("txids") or []
    if txids:
        txid = txids[0]
    if not txid:
        txid = f"blockbook-{address[-12:]}"

    confirmations = 1 if confirmed_sats > 0 else 0

    logger.debug(
        f"[Blockbook] confirmed={confirmed_sats} sats, unconfirmed={unconfirmed_sats} sats, "
        f"conf~{confirmations}, tx={txid[:16]}"
    )

    return {
        "provider": "blockbook",
        "txid": txid,
        "amount_ltc": total_sats / SATOSHIS_PER_LTC,
        "confirmed_ltc": confirmed_sats / SATOSHIS_PER_LTC,
        "confirmations": confirmations,
    }


async def _fetch_litespace(session: aiohttp.ClientSession, address: str):
    logger.debug(f"[LiteSpace] Fetching {address[:20]}...")
    url = f"{LITESPACE_BASE}/api/address/{address}"

    data = await http_get_json(session, url, label="LiteSpace/addr")
    if not data:
        return None

    def _sats(stat, key):
        try:
            return int(stat.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    chain = data.get("chain_stats") or {}
    mempool = data.get("mempool_stats") or {}

    confirmed_sats = max(0, _sats(chain, "funded_txo_sum") - _sats(chain, "spent_txo_sum"))
    unconfirmed_sats = max(0, _sats(mempool, "funded_txo_sum") - _sats(mempool, "spent_txo_sum"))
    total_sats = confirmed_sats + unconfirmed_sats

    if total_sats <= 0:
        return None

    confirmations = 1 if confirmed_sats > 0 else 0

    txid = None
    tx_url = f"{LITESPACE_BASE}/api/address/{address}/txs"
    tx_data = await http_get_json(session, tx_url, label="LiteSpace/txs")
    if tx_data:
        txs = tx_data if isinstance(tx_data, list) else tx_data.get("data", [])
        if txs:
            first = txs[0]
            txid = first.get("txid") or first.get("hash")

    if not txid:
        txid = f"litespace-{address[-12:]}"

    logger.debug(
        f"[LiteSpace] confirmed={confirmed_sats} sats, unconfirmed={unconfirmed_sats} sats, "
        f"conf~{confirmations}, tx={txid[:16]}"
    )

    return {
        "provider": "litespace",
        "txid": txid,
        "amount_ltc": total_sats / SATOSHIS_PER_LTC,
        "confirmed_ltc": confirmed_sats / SATOSHIS_PER_LTC,
        "confirmations": confirmations,
    }


async def fetch_address_info(session: aiohttp.ClientSession, address: str):
    logger.debug(f"Parallel fetch for {address[:20]}...")
    results = await asyncio.gather(
        _fetch_blockcypher(session, address),
        _fetch_sochain(session, address),
        _fetch_litespace(session, address),
        _fetch_blockchair(session, address),
        _fetch_blockbook(session, address),
        return_exceptions=True,
    )

    valid = []
    for i, r in enumerate(results):
        name = PROVIDER_NAMES[i]
        if isinstance(r, Exception):
            logger.debug(f"{name} exception: {r}")
            continue
        if r:
            valid.append(r)
            logger.debug(f"{name}: {r['amount_ltc']:.8f} LTC, {r['confirmations']} conf")
        else:
            logger.debug(f"{name}: no data")

    if len(valid) < MIN_PROVIDERS:
        logger.debug(
            f"Only {len(valid)}/{len(PROVIDER_NAMES)} providers reported a balance "
            f"(need {MIN_PROVIDERS}) — treating as no payment"
        )
        return None

    # Merge: prefer a real txid, then take the highest reported amounts/confirmations
    txid = None
    for r in valid:
        t = r.get("txid")
        if t and not t.startswith(("sochain-", "litespace-", "blockchair-", "blockbook-")):
            txid = t
            break
    if not txid:
        txid = valid[0].get("txid")

    merged = {
        "provider": "merged",
        "txid": txid,
        "amount_ltc": max(r["amount_ltc"] for r in valid),
        "confirmed_ltc": max(r["confirmed_ltc"] for r in valid),
        "confirmations": max(r["confirmations"] for r in valid),
    }
    logger.debug(
        f"Merged ({len(valid)}/{len(PROVIDER_NAMES)} providers): "
        f"{merged['amount_ltc']:.8f} LTC, "
        f"confirmed={merged['confirmed_ltc']:.8f}, {merged['confirmations']} conf"
    )
    return merged


def compare_amount(received: float, expected: float, expected_usd: float = 0, ltc_price: float = 0):
    if expected <= 0:
        return ("error", 0, 0)

    diff = received - expected
    percent = (diff / expected) * 100

    if received >= (expected * OVERPAY_NOTICE_RATIO):
        return ("over", diff, percent)

    if received >= expected:
        return ("pass", diff, percent)

    if expected_usd > 0 and expected > 0:
        received_ratio = received / expected
        received_usd = expected_usd * received_ratio
        shortfall_usd = expected_usd - received_usd

        if shortfall_usd > LOW_AMOUNT_USD_THRESHOLD:
            return ("low", diff, percent)
        else:
            return ("pass", diff, percent)

    if received >= (expected * EXACT_MATCH_RATIO):
        return ("pass", diff, percent)

    return ("low", diff, percent)


async def _send_low_amount_warning(bot, deal, received, expected, txid, percent):
    try:
        channel = bot.get_channel(deal.channel_id)
        if not channel:
            logger.warning(f"Channel {deal.channel_id} not found (low amount)")
            return

        shortfall = expected - received

        embed = discord.Embed(
            title="Low Amount Detected",
            description=(
                f"<@{deal.sender_id}>, you sent a lower amount than required.\n\n"
                f"**Please send the remaining amount to complete this deal.**"
            ),
            color=discord.Color.orange()
        )
        embed.add_field(name="Amount Received", value=f"`{received:.8f} {deal.asset}`", inline=False)
        embed.add_field(name="Required Amount", value=f"`{expected} {deal.asset}`", inline=False)
        embed.add_field(name="Shortfall", value=f"`{shortfall:.8f} {deal.asset}` ({abs(percent):.2f}% short)", inline=False)
        embed.add_field(name="Transaction ID", value=f"`{txid}`", inline=False)
        embed.add_field(
            name="What to do?",
            value=(
                f"1. Send **{shortfall:.8f} {deal.asset}** more to the same address\n"
                f"2. Or contact admin for a refund"
            ),
            inline=False
        )
        embed.set_thumbnail(url="https://cryptologos.cc/logos/litecoin-ltc-logo.png")
        embed.set_footer(text="Crazy MM & Exch")

        await channel.send(embed=embed)
        logger.info(f"Low amount warning sent in channel {deal.channel_id}")

    except Exception as e:
        logger.error(f"Low amount warning error: {e}")


async def check_deal_payment(session: aiohttp.ClientSession, bot: discord.Client, deal):
    logger.debug(f"Checking Deal: {deal.deal_id} | Status: {deal.status}")

    address = deal.deposit_address
    if not address:
        logger.debug(f"Deal {deal.deal_id}: deposit_address NULL")
        return

    expected_ltc = deal.crypto_amount
    if expected_ltc is None or expected_ltc <= 0:
        logger.warning(f"Deal {deal.deal_id}: crypto_amount invalid ({expected_ltc})")
        return

    logger.debug(f"Address: {address}")
    logger.debug(f"Expected: {expected_ltc} {deal.asset} (${deal.usd_amount})")

    info = await fetch_address_info(session, address)
    if not info:
        logger.debug(f"Deal {deal.deal_id}: No payment yet")
        return

    received_ltc = info["amount_ltc"]
    confirmed_ltc = info.get("confirmed_ltc", 0.0)
    txid = info.get("txid")
    confirmations = info.get("confirmations", 0)

    logger.info(
        f"{deal.deal_id} | Received: {received_ltc:.8f} LTC | "
        f"Expected: {expected_ltc} LTC | Conf: {confirmations} | Via: {info['provider']}"
    )

    deal_ltc_price = 0.0
    if deal.usd_amount and deal.crypto_amount and deal.crypto_amount > 0:
        deal_ltc_price = deal.usd_amount / deal.crypto_amount

    decision, diff, percent = compare_amount(
        received_ltc,
        expected_ltc,
        expected_usd=deal.usd_amount or 0,
        ltc_price=deal_ltc_price,
    )
    logger.debug(f"Decision: {decision.upper()} | Diff: {diff:+.8f} LTC ({percent:+.2f}%)")

    from Database import DatabaseManager

    if decision == "low":
        logger.warning(
            f"Deal {deal.deal_id}: LOW AMOUNT — {received_ltc:.8f} LTC "
            f"(expected {expected_ltc} LTC, {abs(percent):.2f}% short)"
        )
        if deal.status != "LOW_AMOUNT":
            await DatabaseManager.update_status(deal.deal_id, "LOW_AMOUNT")
            await _send_low_amount_warning(bot, deal, received_ltc, expected_ltc, txid, percent)
        else:
            logger.debug(f"Deal {deal.deal_id}: Low amount already notified")
        return

    logger.info(f"Deal {deal.deal_id}: Amount OK ({decision.upper()})")

    current_status = deal.status
    amount_fully_confirmed = confirmed_ltc >= (expected_ltc * EXACT_MATCH_RATIO)
    enough_confirmations = confirmations >= REQUIRED_CONFIRMATIONS
    is_confirmed = amount_fully_confirmed and enough_confirmations

    if current_status in ("WAITING_FOR_PAYMENT", "LOW_AMOUNT"):
        safe_txid = txid or f"{address[-16:]}-unknown"
        await DatabaseManager.update_payment_detected(deal.deal_id, safe_txid)
        logger.info(f"DETECTED — Deal {deal.deal_id} | TX: {safe_txid[:16]}...")

        if decision == "over":
            logger.info(f"Deal {deal.deal_id}: OVERPAID by {diff:.8f} LTC ({percent:.2f}%)")

        if is_confirmed:
            await DatabaseManager.update_status(deal.deal_id, "PAYMENT_CONFIRMED")
            logger.info(f"CONFIRMED — Deal {deal.deal_id} | {confirmations} confirmations")
            await send_deal_confirmation(bot, deal, safe_txid, received_ltc, confirmations)
        else:
            await send_payment_detected(
                bot=bot,
                deal=deal,
                txid=safe_txid,
                amount=received_ltc,
                confirmations=confirmations,
                required_confirmations=REQUIRED_CONFIRMATIONS,
            )
        return

    if current_status == "PAYMENT_DETECTED":
        if is_confirmed:
            safe_txid = txid or deal.txid or f"{address[-16:]}-unknown"
            await DatabaseManager.update_status(deal.deal_id, "PAYMENT_CONFIRMED")
            logger.info(f"CONFIRMED — Deal {deal.deal_id} | {confirmations} confirmations")
            await send_deal_confirmation(bot, deal, safe_txid, received_ltc, confirmations)
        else:
            logger.debug(
                f"Deal {deal.deal_id}: {confirmations}/{REQUIRED_CONFIRMATIONS} confirmations"
            )
        return


@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def payment_checker_loop(bot: discord.Client):
    if not bot.is_ready():
        logger.debug("Bot not ready — poll skipped")
        return

    start_time = time.monotonic()
    logger.debug("POLL CYCLE START")

    try:
        from Database import DatabaseManager

        deals_to_check = []
        for status in ("WAITING_FOR_PAYMENT", "PAYMENT_DETECTED", "LOW_AMOUNT"):
            batch = await DatabaseManager.get_deal_by_status(status)
            deals_to_check.extend(batch)
            logger.debug(f"  Status '{status}': {len(batch)} deals")

        if not deals_to_check:
            logger.debug("No deals — loop skipped")
            return

        logger.info(f"{len(deals_to_check)} deals checking (parallel APIs)...")

        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            for deal in deals_to_check:
                try:
                    await check_deal_payment(session, bot, deal)
                except Exception as e:
                    logger.error(f"Deal {deal.deal_id} error: {e}", exc_info=True)
                    continue

        elapsed = time.monotonic() - start_time
        logger.debug(f"POLL CYCLE END ({elapsed:.2f}s)")

    except Exception as e:
        logger.error(f"Loop error: {e}", exc_info=True)


@payment_checker_loop.before_loop
async def before_payment_checker():
    if _bot is None:
        logger.error("Bot reference missing — loop will not start")
        return
    await _bot.wait_until_ready()
    logger.info("Bot ready — Payment Checker loop started.")


@payment_checker_loop.error
async def on_payment_checker_error(error):
    logger.error(f"Payment Checker loop crashed: {error}", exc_info=True)


@tasks.loop(seconds=TRANSCRIPT_INTERVAL_SECONDS)
async def transcript_loop(bot: discord.Client, process_func):
    if not bot.is_ready():
        return
    try:
        await process_func(bot)
    except Exception as e:
        logger.error(f"Transcript loop error: {e}", exc_info=True)


@transcript_loop.before_loop
async def before_transcript_loop():
    if _bot is None:
        return
    await _bot.wait_until_ready()
    logger.info("Bot ready — Transcript loop started.")


@transcript_loop.error
async def on_transcript_loop_error(error):
    logger.error(f"Transcript loop crashed: {error}", exc_info=True)


@tasks.loop(seconds=CHANNEL_DELETE_CHECK_INTERVAL)
async def channel_cleanup_loop(bot: discord.Client):
    if not bot.is_ready():
        return

    try:
        from Database import DatabaseManager

        deals = await DatabaseManager.get_deals_due_for_deletion()
        if not deals:
            return

        logger.info(f"Found {len(deals)} channel(s) due for deletion.")

        for deal in deals:
            try:
                channel = bot.get_channel(deal.channel_id)
                if channel:
                    try:
                        await channel.send(
                            "⏳ This deal is completed. Channel will be deleted shortly."
                        )
                        await asyncio.sleep(5)
                    except Exception:
                        pass

                    try:
                        await channel.delete(reason="Deal completed — auto cleanup")
                        logger.info(f"Channel deleted: {deal.channel_id} | Deal: {deal.deal_id}")
                    except discord.NotFound:
                        logger.warning(f"Channel already deleted: {deal.channel_id}")
                    except discord.Forbidden:
                        logger.error(f"No permission to delete channel {deal.channel_id}")
                    except Exception as e:
                        logger.error(f"Channel delete failed: {e}")
                else:
                    logger.warning(f"Channel {deal.channel_id} not found — skipping delete")

                await DatabaseManager.decrement_active_channels(
                    [deal.sender_id, deal.receiver_id]
                )
                await DatabaseManager.clear_channel_delete(deal.deal_id)

            except Exception as e:
                logger.error(f"Channel cleanup error for {deal.deal_id}: {e}")
                continue

    except Exception as e:
        logger.error(f"Channel cleanup loop error: {e}", exc_info=True)


@channel_cleanup_loop.before_loop
async def before_channel_cleanup():
    if _bot is None:
        return
    await _bot.wait_until_ready()
    logger.info("Bot ready — Channel cleanup loop started.")


@channel_cleanup_loop.error
async def on_channel_cleanup_error(error):
    logger.error(f"Channel cleanup loop crashed: {error}", exc_info=True)


@tasks.loop(seconds=TASK_QUEUE_INTERVAL)
async def task_queue_loop(bot: discord.Client):
    if not bot.is_ready():
        return

    try:
        from Database import DatabaseManager

        tasks_list = await DatabaseManager.get_pending_tasks(limit=5)
        if not tasks_list:
            return

        logger.info(f"Processing {len(tasks_list)} pending task(s)...")

        for task in tasks_list:
            task_id = task["id"]
            task_type = task["task_type"]
            try:
                payload = json.loads(task["payload"])
            except Exception as e:
                logger.error(f"Task {task_id} payload parse failed: {e}")
                await DatabaseManager.mark_task_failed(task_id, f"Payload parse: {e}")
                continue

            try:
                if task_type == "post_transaction_dm":
                    await _process_post_transaction_dm(bot, payload)
                else:
                    logger.warning(f"Unknown task type: {task_type}")
                    await DatabaseManager.mark_task_failed(task_id, f"Unknown type: {task_type}")
                    continue

                await DatabaseManager.mark_task_complete(task_id)

            except Exception as e:
                logger.error(f"Task {task_id} ({task_type}) failed: {e}", exc_info=True)
                await DatabaseManager.mark_task_failed(task_id, str(e))

    except Exception as e:
        logger.error(f"Task queue loop error: {e}", exc_info=True)


@task_queue_loop.before_loop
async def before_task_queue():
    if _bot is None:
        return
    await _bot.wait_until_ready()
    logger.info("Bot ready — Task queue loop started.")


@task_queue_loop.error
async def on_task_queue_error(error):
    logger.error(f"Task queue loop crashed: {error}", exc_info=True)


async def _process_post_transaction_dm(bot: discord.Client, payload: dict):
    from Database import DatabaseManager

    deal_id = payload.get("deal_id")
    txid = payload.get("txid")
    send_amount = payload.get("send_amount")
    fee_amount = payload.get("fee_amount")
    recipient = payload.get("recipient")
    kind = payload.get("kind", "RELEASE")
    only_for = payload.get("only_for")

    deal = await DatabaseManager.get_deal_by_id(deal_id)
    if not deal:
        logger.warning(f"Deal not found for post-transaction task: {deal_id}")
        return

    transcript_path = None

    # Try to find existing transcript on disk first (hybrid may have already created it)
    try:
        possible = DETAIL_BACKUP_DIR / deal_id / "transcript.html"
        if possible.exists():
            transcript_path = str(possible)
            logger.debug(f"Found existing transcript: {transcript_path}")
    except Exception as e:
        logger.debug(f"Transcript check failed: {e}")

    # If only_for is present, this is a hybrid fallback — skip full pipeline
    if not only_for:
        logger.info(f"Full pipeline for {deal_id} (no only_for)")

        try:
            from backup_manager.transcript_manager import finalize_transcript
            new_path = await finalize_transcript(bot, deal)
            if new_path:
                transcript_path = new_path
                logger.info(f"Transcript finalized for {deal_id}: {transcript_path}")
        except Exception as e:
            logger.error(f"Transcript finalization failed for {deal_id}: {e}")

        try:
            from backup_manager.backup_manager import get_backup_manager
            bm = get_backup_manager()

            await bm.update_extended_backup(
                deal_id,
                {
                    "status": "COMPLETED" if kind == "RELEASE" else "REFUNDED",
                    "finalized_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "transcript_file": "transcript.html" if transcript_path else None,
                    "transaction": {
                        "txid": txid,
                        "amount": send_amount,
                        "fee": fee_amount,
                        "recipient": recipient,
                        "kind": kind,
                    },
                },
            )

            asyncio.create_task(bm.upload_extended_to_clouds(deal_id))
        except Exception as e:
            logger.error(f"Extended backup update failed for {deal_id}: {e}")
    else:
        logger.info(f"Fallback pipeline for {deal_id} | only_for={only_for}")
        logger.debug(f"Skipping transcript finalize + extended backup (already done by hybrid)")

    # Determine recipients
    targets = []

    if only_for:
        if deal.sender_id and deal.sender_id in only_for:
            targets.append((deal.sender_id, "Buyer"))
        if deal.receiver_id and deal.receiver_id in only_for:
            targets.append((deal.receiver_id, "Seller"))
    else:
        if deal.sender_id:
            targets.append((deal.sender_id, "Buyer"))
        if deal.receiver_id:
            targets.append((deal.receiver_id, "Seller"))

    if not targets:
        logger.warning(f"No DM targets for {deal_id} (only_for={only_for})")
        return

    for uid, role_label in targets:
        try:
            await _send_dm_to_user(
                bot=bot,
                user_id=uid,
                deal=deal,
                txid=txid,
                send_amount=send_amount,
                fee_amount=fee_amount,
                recipient=recipient,
                kind=kind,
                transcript_path=transcript_path,
                role_label=role_label,
            )
        except Exception as e:
            logger.error(f"DM to {uid} failed in queue task: {e}")


async def _send_dm_to_user(
    bot: discord.Client,
    user_id: int,
    deal,
    txid: str,
    send_amount: float,
    fee_amount: float,
    recipient: str,
    kind: str,
    transcript_path: Optional[str],
    role_label: str,
):
    if not user_id:
        return

    try:
        user = await bot.fetch_user(user_id)
    except Exception as e:
        logger.warning(f"Could not fetch user {user_id}: {e}")
        return

    try:
        if kind == "RELEASE":
            title = "✅ Deal Completed — Funds Released"
            description = (
                f"Your escrow deal has been successfully completed.\n"
                f"Funds have been released to the seller.\n\n"
                f"**Deal Details**"
            )
            amount_label = "Amount Sent"
        else:
            title = "✅ Deal Cancelled — Funds Refunded"
            description = (
                f"Your escrow deal has been cancelled.\n"
                f"Funds have been refunded to the buyer.\n\n"
                f"**Deal Details**"
            )
            amount_label = "Amount Refunded"

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.green(),
        )
        embed.add_field(name="Deal ID", value=f"`{deal.deal_id}`", inline=False)
        embed.add_field(name="Your Role", value=role_label, inline=True)
        embed.add_field(name="Asset", value=deal.asset, inline=True)
        embed.add_field(
            name=amount_label,
            value=f"`{send_amount:.8f} {deal.asset}`",
            inline=True,
        )
        embed.add_field(
            name="Network Fee",
            value=f"`{fee_amount:.8f} {deal.asset}`",
            inline=True,
        )
        embed.add_field(name="Recipient", value=f"`{recipient}`", inline=False)
        embed.add_field(
            name="Transaction ID",
            value=f"`{txid}`\n[View on Blockchain](https://live.blockcypher.com/ltc/tx/{txid})",
            inline=False,
        )
        embed.set_footer(text="Crazy MM & Exchange")

        files = []
        if transcript_path:
            path = Path(transcript_path)
            if path.exists():
                size_mb = path.stat().st_size / (1024 * 1024)
                if size_mb <= 8.0:
                    files.append(discord.File(str(path), filename=f"{deal.deal_id}-transcript.html"))
                else:
                    logger.warning(f"Transcript too large ({size_mb:.2f} MB) — skipping attachment")
                    embed.add_field(
                        name="Transcript",
                        value=f"⚠️ Transcript file too large ({size_mb:.2f} MB) — request from admin.",
                        inline=False,
                    )
            else:
                logger.warning(f"Transcript path does not exist: {path}")

        await user.send(embed=embed, files=files if files else [])
        logger.info(f"DM sent to {user_id} for deal {deal.deal_id}")

    except discord.Forbidden:
        logger.warning(f"Cannot DM user {user_id} (DMs blocked).")
    except Exception as e:
        logger.error(f"DM failed for user {user_id} | Deal {deal.deal_id}: {e}")