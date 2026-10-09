import os
import time
import hashlib
import hmac
import logging
import discord
from discord import ui
from typing import Callable, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("AdminUtils")

ADMIN_PIN_HASH = os.getenv("ADMIN_PIN_HASH", "").strip()
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "").strip()
PIN_MAX_ATTEMPTS_PER_MINUTE = int(os.getenv("PIN_MAX_ATTEMPTS_PER_MINUTE", "5"))

SESSION_TTL_SECONDS = 600

BRAND_COLOR = discord.Color(0x0303d4)

_admin_user_ids = set()
try:
    if ADMIN_USER_IDS_RAW:
        for raw in ADMIN_USER_IDS_RAW.split(","):
            raw = raw.strip()
            if raw.isdigit():
                _admin_user_ids.add(int(raw))
except Exception as e:
    logger.error(f"Failed to parse ADMIN_USER_IDS: {e}")

_sessions: dict = {}


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(a, b)
    except Exception:
        return False


def is_admin_user(user_id: int) -> bool:
    if not _admin_user_ids:
        logger.warning("ADMIN_USER_IDS empty — no admins configured.")
        return False
    return user_id in _admin_user_ids


def has_valid_session(user_id: int) -> bool:
    entry = _sessions.get(user_id)
    if not entry:
        return False
    if time.time() - entry > SESSION_TTL_SECONDS:
        _sessions.pop(user_id, None)
        return False
    return True


def grant_session(user_id: int):
    _sessions[user_id] = time.time()
    logger.info(f"Admin session granted for {user_id}")


def revoke_session(user_id: int):
    _sessions.pop(user_id, None)
    logger.info(f"Admin session revoked for {user_id}")


def clear_session(user_id: int):
    revoke_session(user_id)


class PinModal(ui.Modal, title="Admin Verification"):
    pin_input = ui.TextInput(
        label="Enter Admin PIN",
        placeholder="Enter your PIN",
        required=True,
        max_length=32,
        custom_id="admin_pin_input",
    )

    def __init__(self, owner_id: int, callback: Callable, callback_args: tuple, callback_kwargs: dict):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self._callback = callback
        self._callback_args = callback_args
        self._callback_kwargs = callback_kwargs

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This verification is not for you.",
                ephemeral=True,
            )
            return

        try:
            from Database import DatabaseManager
        except ImportError as e:
            logger.error(f"Database import failed in PinModal: {e}")
            await interaction.response.send_message(
                "❌ Internal error. Contact admin.",
                ephemeral=True,
            )
            return

        allowed, err_msg = await DatabaseManager.check_pin_rate_limit(
            interaction.user.id,
            PIN_MAX_ATTEMPTS_PER_MINUTE,
        )

        if not allowed:
            logger.warning(f"PIN rate limit hit for {interaction.user.id}")
            await interaction.response.send_message(err_msg, ephemeral=True)
            return

        provided = self.pin_input.value.strip()

        if not ADMIN_PIN_HASH:
            logger.error("ADMIN_PIN_HASH not set in .env")
            await interaction.response.send_message(
                "❌ Admin PIN not configured on server.",
                ephemeral=True,
            )
            return

        if not constant_time_compare(hash_pin(provided), ADMIN_PIN_HASH):
            logger.warning(f"Invalid PIN attempt by user {interaction.user.id}")

            still_allowed, _ = await DatabaseManager.check_pin_rate_limit(
                interaction.user.id,
                PIN_MAX_ATTEMPTS_PER_MINUTE,
            )

            if not still_allowed:
                await interaction.response.send_message(
                    f"🔒 **Too many failed attempts.** Please wait 1 minute.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Invalid PIN.",
                    ephemeral=True,
                )
            return

        await DatabaseManager.reset_pin_attempts(interaction.user.id)
        grant_session(interaction.user.id)
        logger.info(f"Admin PIN verified | user={interaction.user.id}")

        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        try:
            await self._callback(interaction, *self._callback_args, **self._callback_kwargs)
        except Exception as e:
            logger.error(f"Admin callback error: {e}", exc_info=True)
            try:
                await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass


async def admin_pin_required(interaction: discord.Interaction, callback: Callable, *args, **kwargs):
    if not is_admin_user(interaction.user.id):
        await interaction.response.send_message(
            "❌ You are not authorized to use this command.",
            ephemeral=True,
        )
        return

    if has_valid_session(interaction.user.id):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        try:
            await callback(interaction, *args, **kwargs)
        except Exception as e:
            logger.error(f"Admin callback error: {e}", exc_info=True)
            try:
                await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass
        return

    try:
        from Database import DatabaseManager
    except ImportError as e:
        logger.error(f"Database import failed: {e}")
        await interaction.response.send_message(
            "❌ Internal error. Contact admin.",
            ephemeral=True,
        )
        return

    allowed, err_msg = await DatabaseManager.check_pin_rate_limit(
        interaction.user.id,
        PIN_MAX_ATTEMPTS_PER_MINUTE,
    )

    if not allowed:
        await interaction.response.send_message(err_msg, ephemeral=True)
        return

    modal = PinModal(
        owner_id=interaction.user.id,
        callback=callback,
        callback_args=args,
        callback_kwargs=kwargs,
    )
    await interaction.response.send_modal(modal)


def get_admin_user_ids() -> set:
    return set(_admin_user_ids)


def get_active_sessions_count() -> int:
    now = time.time()
    active = [uid for uid, ts in _sessions.items() if now - ts < SESSION_TTL_SECONDS]
    return len(active)


def cleanup_sessions():
    now = time.time()
    expired = [uid for uid, ts in _sessions.items() if now - ts >= SESSION_TTL_SECONDS]
    for uid in expired:
        _sessions.pop(uid, None)
    if expired:
        logger.info(f"Cleaned up {len(expired)} expired admin session(s).")