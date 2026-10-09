import discord
from discord import app_commands
from discord.ext import commands
import os
import logging
import asyncio
from logging.handlers import TimedRotatingFileHandler
from dotenv import load_dotenv

os.makedirs("logs", exist_ok=True)

import Database
from Database import DatabaseManager
import Backgroundtask
import RoleSelection
import Invoice
from wallet import WalletManager
from DealConfirmation import DealConfirmationView
from ui.PaymentDetected import PaymentDetectedView

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

if root_logger.handlers:
    root_logger.handlers.clear()

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_fmt = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
console_handler.setFormatter(console_fmt)
root_logger.addHandler(console_handler)

main_file_handler = TimedRotatingFileHandler(
    "logs/bot.log",
    when="midnight",
    interval=1,
    backupCount=2,
    encoding="utf-8",
)
main_file_handler.setLevel(logging.INFO)
main_fmt = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
main_file_handler.setFormatter(main_fmt)
root_logger.addHandler(main_file_handler)

logger = logging.getLogger("MainBot")

try:
    from backup_manager import get_backup_manager, BackupManager
    _BACKUP_OK = True
except ImportError as e:
    _BACKUP_OK = False
    get_backup_manager = None
    BackupManager = None
    logger.warning(f"backup_manager module missing: {e}")

try:
    from security.admin_utils import (
        is_admin_user,
        admin_pin_required,
    )
    _SECURITY_OK = True
except ImportError as e:
    _SECURITY_OK = False
    is_admin_user = None
    admin_pin_required = None
    logger.warning(f"security.admin_utils missing: {e}")

try:
    from buttons.release import force_release_deal
    _FORCE_RELEASE_OK = True
except ImportError as e:
    _FORCE_RELEASE_OK = False
    force_release_deal = None
    logger.warning(f"buttons.release missing: {e}")

try:
    from buttons.cancel import force_cancel_deal
    _FORCE_CANCEL_OK = True
except ImportError as e:
    _FORCE_CANCEL_OK = False
    force_cancel_deal = None
    logger.warning(f"buttons.cancel missing: {e}")


class EscrowBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.guilds = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Crazy MM Escrow"
            ),
            status=discord.Status.online
        )
        self.backup_manager = None

    async def setup_hook(self):
        logger.info("Bot setup shuru ho raha hai...")

        try:
            self.wallet = WalletManager()
            logger.info("Wallet Manager initialized.")
        except Exception as e:
            logger.critical(f"Wallet init failed: {e}")
            return

        if _BACKUP_OK:
            try:
                self.backup_manager = get_backup_manager()
                logger.info("Backup Manager initialized.")
            except Exception as e:
                logger.error(f"Backup Manager init failed: {e}")
        else:
            logger.warning("Backup Manager skipped (module missing)")

        try:
            await DatabaseManager.init_db()
            logger.info("Database initialized.")
        except Exception as e:
            logger.critical(f"Database init failed: {e}")
            return

        try:
            Backgroundtask.start_payment_checker(self)
            logger.info("Payment checker started.")
        except Exception as e:
            logger.error(f"Background task failed: {e}")

        try:
            self.add_view(RoleSelection.SetupMMPanel())
            self.add_view(RoleSelection.RoleView())
            self.add_view(RoleSelection.UserCheckView())
            self.add_view(RoleSelection.ConfirmAmountView())
            self.add_view(Invoice.InvoiceView())
            self.add_view(PaymentDetectedView())
            self.add_view(DealConfirmationView())
            logger.info("Persistent views registered.")
        except Exception as e:
            logger.error(f"Views register failed: {e}", exc_info=True)

        try:
            from security.admin_utils import cleanup_sessions
            cleanup_sessions()
            logger.info("Admin session cleanup done.")
        except Exception as e:
            logger.warning(f"Session cleanup failed: {e}")

        try:
            from backup_manager.transcript_manager import cleanup_orphan_temps
            deleted = await cleanup_orphan_temps()
            logger.info(f"Orphan transcript cleanup: {deleted} file(s) removed.")
        except ImportError:
            logger.debug("Transcript cleanup skipped (module missing)")
        except Exception as e:
            logger.warning(f"Transcript cleanup failed: {e}")

        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info(f"{len(synced)} slash commands synced to guild {GUILD_ID}.")
            else:
                synced = await self.tree.sync()
                logger.info(f"{len(synced)} slash commands synced globally.")
        except Exception as e:
            logger.error(f"Command sync failed: {e}")

    async def on_ready(self):
        logger.info(f"Bot {self.user} (ID: {self.user.id}) online.")
        logger.info(f"Servers: {len(self.guilds)}")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Crazy MM Escrow"
            )
        )

    async def close(self):
        logger.info("Bot shutdown initiated...")
        if self.backup_manager:
            try:
                await self.backup_manager.close()
                logger.info("BackupManager closed.")
            except Exception as e:
                logger.error(f"BackupManager close error: {e}")
        await super().close()

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        logger.error(f"Command error: {error}")


bot = EscrowBot()


async def _check_admin_id(interaction: discord.Interaction) -> bool:
    if not _SECURITY_OK:
        await interaction.response.send_message(
            "❌ Admin commands disabled (security module not available).",
            ephemeral=True,
        )
        return False

    if not is_admin_user(interaction.user.id):
        await interaction.response.send_message(
            "❌ You are not authorized to use this command.",
            ephemeral=True,
        )
        return False

    return True


async def _check_admin_id_and_pin(interaction: discord.Interaction, callback, *args, **kwargs):
    if not _SECURITY_OK:
        await interaction.response.send_message(
            "❌ Admin commands disabled (security module not available).",
            ephemeral=True,
        )
        return

    if not is_admin_user(interaction.user.id):
        await interaction.response.send_message(
            "❌ You are not authorized to use this command.",
            ephemeral=True,
        )
        return

    await admin_pin_required(interaction, callback, *args, **kwargs)


@bot.tree.command(name="setup_mm", description="Setup the Auto MM Escrow Panel")
@app_commands.default_permissions(administrator=True)
async def setup_mm(interaction: discord.Interaction):
    try:
        await interaction.response.send_message(view=RoleSelection.SetupMMPanel())
        logger.info(f"Setup panel sent to channel {interaction.channel_id}.")
    except Exception as e:
        logger.error(f"Setup panel error: {e}", exc_info=True)
        try:
            await interaction.response.send_message("❌ Error sending panel.", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! `{latency}ms`", ephemeral=True)


@bot.tree.command(name="help", description="Show bot commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ Crazy MM & Exchange — Help",
        color=discord.Color(0x0303d4),
    )
    embed.add_field(name="/setup_mm", value="Setup the escrow panel (Admin).", inline=False)
    embed.add_field(name="/ping", value="Check bot latency.", inline=False)
    embed.add_field(name="/help", value="Show help.", inline=False)
    embed.add_field(name="/backup_status", value="Backup stats (Admin).", inline=False)
    embed.add_field(name="/force-release", value="Force-release funds (Admin + PIN).", inline=False)
    embed.add_field(name="/force-cancel", value="Force-cancel deal (Admin + PIN).", inline=False)
    embed.set_footer(text="Crazy MM & Exch • Powered by AutoMM")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="backup_status", description="Show backup system statistics (Admin)")
@app_commands.default_permissions(administrator=True)
async def backup_status(interaction: discord.Interaction):
    if not await _check_admin_id(interaction):
        return

    try:
        await interaction.response.defer(ephemeral=True)
        stats = await DatabaseManager.get_backup_stats()

        embed = discord.Embed(
            title="📦 Backup System Status",
            color=discord.Color(0x0303d4),
        )
        embed.add_field(name="Total", value=f"`{stats.get('total', 0)}`", inline=True)
        embed.add_field(name="✅ Complete", value=f"`{stats.get('complete', 0)}`", inline=True)
        embed.add_field(name="⚠️ Partial", value=f"`{stats.get('partial', 0)}`", inline=True)
        embed.add_field(name="⏳ Pending", value=f"`{stats.get('pending', 0)}`", inline=True)
        embed.add_field(name="❌ Failed", value=f"`{stats.get('failed', 0)}`", inline=True)
        embed.set_footer(text="Crazy MM & Exch • Backup Dashboard")

        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"backup_status error: {e}", exc_info=True)
        try:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(
    name="force-release",
    description="Force-release funds to seller (Admin + PIN). Optional: deal_id",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(deal_id="Optional: Deal ID. If omitted, uses current channel's deal.")
async def force_release(interaction: discord.Interaction, deal_id: str = None):
    if not _FORCE_RELEASE_OK:
        await interaction.response.send_message(
            "❌ Force-release is not available (module missing).",
            ephemeral=True,
        )
        return

    async def _impl(itx: discord.Interaction):
        try:
            await force_release_deal(itx, deal_id)
        except Exception as e:
            logger.error(f"force_release error: {e}", exc_info=True)
            try:
                await itx.followup.send(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass

    await _check_admin_id_and_pin(interaction, _impl)


@bot.tree.command(
    name="force-cancel",
    description="Force-cancel deal and refund buyer (Admin + PIN). Optional: deal_id",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(deal_id="Optional: Deal ID. If omitted, uses current channel's deal.")
async def force_cancel(interaction: discord.Interaction, deal_id: str = None):
    if not _FORCE_CANCEL_OK:
        await interaction.response.send_message(
            "❌ Force-cancel is not available (module missing).",
            ephemeral=True,
        )
        return

    async def _impl(itx: discord.Interaction):
        try:
            await force_cancel_deal(itx, deal_id)
        except Exception as e:
            logger.error(f"force_cancel error: {e}", exc_info=True)
            try:
                await itx.followup.send(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass

    await _check_admin_id_and_pin(interaction, _impl)


@bot.tree.command(
    name="backup_stats_deep",
    description="Deep backup stats (Admin + PIN)",
)
@app_commands.default_permissions(administrator=True)
async def backup_stats_deep(interaction: discord.Interaction):
    if not _SECURITY_OK:
        await interaction.response.send_message("❌ Security module missing.", ephemeral=True)
        return

    async def _impl(itx: discord.Interaction):
        try:
            stats = await DatabaseManager.get_backup_stats()

            try:
                from backup_manager.transcript_manager import get_transcript_stats
                t_stats = await get_transcript_stats()
            except Exception:
                t_stats = {}

            embed = discord.Embed(
                title="📊 Deep Backup Statistics",
                color=discord.Color(0x0303d4),
            )
            embed.add_field(name="Total Backups", value=f"`{stats.get('total', 0)}`", inline=True)
            embed.add_field(name="✅ Complete", value=f"`{stats.get('complete', 0)}`", inline=True)
            embed.add_field(name="⚠️ Partial", value=f"`{stats.get('partial', 0)}`", inline=True)
            embed.add_field(name="⏳ Pending", value=f"`{stats.get('pending', 0)}`", inline=True)
            embed.add_field(name="❌ Failed", value=f"`{stats.get('failed', 0)}`", inline=True)

            if t_stats:
                embed.add_field(name="— Transcripts —", value="\u200b", inline=False)
                embed.add_field(name="Temp Files", value=f"`{t_stats.get('temp_files', 0)}`", inline=True)
                embed.add_field(name="Temp Size", value=f"`{t_stats.get('temp_size_kb', 0)} KB`", inline=True)
                embed.add_field(name="Final Files", value=f"`{t_stats.get('final_files', 0)}`", inline=True)
                embed.add_field(name="Final Size", value=f"`{t_stats.get('final_size_kb', 0)} KB`", inline=True)
                embed.add_field(
                    name="chat_exporter",
                    value="✅ OK" if t_stats.get("chat_exporter_ok") else "❌ Missing",
                    inline=True,
                )

            embed.set_footer(text="Crazy MM & Exch • Deep Stats")
            await itx.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"backup_stats_deep error: {e}", exc_info=True)
            try:
                await itx.followup.send(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass

    await _check_admin_id_and_pin(interaction, _impl)


if __name__ == "__main__":
    if not TOKEN:
        logger.critical("DISCORD_TOKEN missing in .env")
    else:
        try:
            logger.info("Starting bot...")
            bot.run(TOKEN)
        except discord.LoginFailure:
            logger.critical("Invalid Discord token.")
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
        except Exception as e:
            logger.critical(f"Bot crashed: {e}", exc_info=True)