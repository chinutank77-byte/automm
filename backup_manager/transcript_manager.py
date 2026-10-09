import os
import logging
import asyncio
import aiofiles
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List

import discord

try:
    import chat_exporter
    CHAT_EXPORTER_OK = True
except ImportError:
    CHAT_EXPORTER_OK = False

logger = logging.getLogger("TranscriptManager")

BASE_DIR = Path(__file__).resolve().parent.parent
DETAIL_BACKUP_DIR = BASE_DIR / "detail_backup"
DETAIL_TEMP_DIR = DETAIL_BACKUP_DIR / "temp"

TRANSCRIPT_TZ = "UTC"
TRANSCRIPT_LIMIT = 5000
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024
TRANSCRIPT_TIMEOUT = 30
MAX_RETRIES = 2
RETRY_DELAY = 3

ACTIVE_STATUSES = (
    "PAYMENT_CONFIRMED",
    "RELEASE_APPROVED",
    "RELEASING",
    "CANCEL_APPROVED",
    "REFUNDING",
)


def _ensure_temp_dir():
    try:
        DETAIL_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        DETAIL_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Transcript dirs creation failed: {e}")


def _temp_path(deal_id: str, seller_id) -> Path:
    return DETAIL_TEMP_DIR / f"{deal_id}.{seller_id or 'unknown'}.html"


def _final_path(deal_id: str) -> Path:
    return DETAIL_BACKUP_DIR / deal_id / "transcript.html"


def _deal_dir(deal_id: str) -> Path:
    return DETAIL_BACKUP_DIR / deal_id


async def _generate_html(channel: discord.TextChannel) -> Optional[str]:
    if not CHAT_EXPORTER_OK:
        logger.warning("chat_exporter not installed.")
        return None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            html = await asyncio.wait_for(
                chat_exporter.export(
                    channel,
                    limit=TRANSCRIPT_LIMIT,
                    tz_info=TRANSCRIPT_TZ,
                ),
                timeout=TRANSCRIPT_TIMEOUT,
            )
            if html:
                return html
            return None

        except asyncio.TimeoutError:
            logger.warning(
                f"Transcript timeout (attempt {attempt}/{MAX_RETRIES}) | "
                f"channel={channel.id}"
            )
        except discord.HTTPException as e:
            logger.warning(
                f"Transcript HTTP error (attempt {attempt}/{MAX_RETRIES}): {e}"
            )
        except Exception as e:
            logger.error(
                f"Transcript generation error (attempt {attempt}/{MAX_RETRIES}): {e}",
                exc_info=True,
            )

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY * attempt)

    logger.error(f"Transcript failed after {MAX_RETRIES} attempts | channel={channel.id}")
    return None


async def _write_file(path: Path, content: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(content)
        return True
    except Exception as e:
        logger.error(f"Write failed for {path}: {e}")
        return False


async def _safe_delete(path: Path) -> bool:
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception as e:
        logger.warning(f"Delete failed for {path}: {e}")
    return False


async def _update_temp_transcript(bot: discord.Client, deal) -> bool:
    channel = bot.get_channel(deal.channel_id)
    if not channel:
        logger.debug(f"Channel {deal.channel_id} not found for {deal.deal_id}")
        return False

    seller_id = deal.receiver_id or "unknown"
    temp_path = _temp_path(deal.deal_id, seller_id)

    await _safe_delete(temp_path)

    html = await _generate_html(channel)
    if not html:
        return False

    if len(html.encode("utf-8")) > MAX_FILE_SIZE_BYTES:
        logger.warning(
            f"Transcript too large for {deal.deal_id} "
            f"({len(html)} bytes) — skipping"
        )
        return False

    ok = await _write_file(temp_path, html)
    if ok:
        logger.debug(f"Temp transcript updated: {temp_path.name}")
    return ok


async def process_active_transcripts(bot: discord.Client):
    if not CHAT_EXPORTER_OK:
        return

    _ensure_temp_dir()

    try:
        from Database import DatabaseManager
    except ImportError as e:
        logger.warning(f"Database import failed: {e}")
        return

    deals: List = []
    try:
        for status in ACTIVE_STATUSES:
            batch = await DatabaseManager.get_deal_by_status(status)
            deals.extend(batch)
    except Exception as e:
        logger.error(f"Failed to fetch active deals: {e}")
        return

    if not deals:
        return

    logger.debug(f"Processing transcripts for {len(deals)} deal(s)...")

    for deal in deals:
        try:
            await _update_temp_transcript(bot, deal)
        except Exception as e:
            logger.error(f"Transcript processing failed for {deal.deal_id}: {e}")
            continue
        await asyncio.sleep(0.5)


async def finalize_transcript(bot: discord.Client, deal) -> Optional[str]:
    _ensure_temp_dir()

    channel = bot.get_channel(deal.channel_id)
    seller_id = deal.receiver_id or "unknown"
    temp_path = _temp_path(deal.deal_id, seller_id)
    final_path = _final_path(deal.deal_id)

    html = None

    if channel:
        html = await _generate_html(channel)

    if not html and temp_path.exists():
        try:
            async with aiofiles.open(temp_path, "r", encoding="utf-8") as f:
                html = await f.read()
            logger.info(f"Used cached temp transcript for {deal.deal_id}")
        except Exception as e:
            logger.warning(f"Failed to read temp transcript: {e}")
            html = None

    if not html:
        logger.warning(f"No transcript content for {deal.deal_id}")
        await _safe_delete(temp_path)
        return None

    deal_dir = _deal_dir(deal.deal_id)
    deal_dir.mkdir(parents=True, exist_ok=True)

    ok = await _write_file(final_path, html)
    if not ok:
        return None

    await _safe_delete(temp_path)

    try:
        size_kb = final_path.stat().st_size / 1024
        logger.info(
            f"Final transcript saved: {final_path.name} ({size_kb:.1f} KB) | "
            f"Deal: {deal.deal_id}"
        )
    except Exception:
        logger.info(f"Final transcript saved: {final_path.name} | Deal: {deal.deal_id}")

    return str(final_path)


async def cleanup_orphan_temps() -> int:
    _ensure_temp_dir()

    if not DETAIL_TEMP_DIR.exists():
        return 0

    try:
        from Database import DatabaseManager
    except ImportError:
        logger.warning("Database not available — orphan cleanup skipped.")
        return 0

    active_deal_ids = set()
    try:
        for status in ACTIVE_STATUSES:
            deals = await DatabaseManager.get_deal_by_status(status)
            for d in deals:
                active_deal_ids.add(d.deal_id)

        for status in ("COMPLETED", "CANCELLED", "REFUNDED", "RELEASE_FAILED", "REFUND_FAILED"):
            deals = await DatabaseManager.get_deal_by_status(status)
            for d in deals:
                active_deal_ids.add(d.deal_id)

    except Exception as e:
        logger.error(f"Cleanup status fetch failed: {e}")
        return 0

    deleted = 0
    for temp_file in DETAIL_TEMP_DIR.iterdir():
        if not temp_file.is_file():
            continue

        parts = temp_file.stem.split(".")
        if not parts:
            continue

        deal_id = parts[0]
        if deal_id not in active_deal_ids:
            if await _safe_delete(temp_file):
                deleted += 1

    if deleted:
        logger.info(f"Cleaned up {deleted} orphan transcript temp(s).")
    else:
        logger.debug("No orphan transcript temp files found.")

    return deleted


async def get_transcript_stats() -> dict:
    _ensure_temp_dir()

    stats = {
        "temp_files": 0,
        "temp_size_kb": 0.0,
        "final_files": 0,
        "final_size_kb": 0.0,
        "chat_exporter_ok": CHAT_EXPORTER_OK,
    }

    try:
        if DETAIL_TEMP_DIR.exists():
            for f in DETAIL_TEMP_DIR.iterdir():
                if f.is_file():
                    stats["temp_files"] += 1
                    stats["temp_size_kb"] += f.stat().st_size / 1024

        if DETAIL_BACKUP_DIR.exists():
            for deal_folder in DETAIL_BACKUP_DIR.iterdir():
                if not deal_folder.is_dir():
                    continue
                transcript = deal_folder / "transcript.html"
                if transcript.exists():
                    stats["final_files"] += 1
                    stats["final_size_kb"] += transcript.stat().st_size / 1024

        stats["temp_size_kb"] = round(stats["temp_size_kb"], 2)
        stats["final_size_kb"] = round(stats["final_size_kb"], 2)

    except Exception as e:
        logger.error(f"get_transcript_stats failed: {e}")

    return stats


async def force_transcript_for_deal(bot: discord.Client, deal_id: str) -> Optional[str]:
    try:
        from Database import DatabaseManager
    except ImportError:
        return None

    deal = await DatabaseManager.get_deal_by_id(deal_id)
    if not deal:
        logger.warning(f"Deal not found for transcript: {deal_id}")
        return None

    return await finalize_transcript(bot, deal)


async def cleanup_final_transcript(bot: discord.Client, deal):
    from Database import DatabaseManager

    try:
        seller_id = deal.receiver_id or "unknown"
        temp_path = _temp_path(deal.deal_id, seller_id)
        await _safe_delete(temp_path)

        await DatabaseManager.update_status(deal.deal_id, "TRANSCRIPT_SAVED")

        logger.info(f"Transcript cleanup done for {deal.deal_id}")

    except Exception as e:
        logger.error(f"cleanup_final_transcript error for {deal.deal_id}: {e}")