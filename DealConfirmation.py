import discord
from discord import ui
import os
import logging
import datetime
from dotenv import load_dotenv

load_dotenv()

if discord.version_info < (2, 4):
    raise ImportError(
        "discord.py 2.4+ required for Components V2. "
        "Run: pip install -U discord.py"
    )

logger = logging.getLogger("DealConfirmation")

BRAND_COLOR = discord.Color(0x0303d4)
EXPLORER_BASE = "https://live.blockcypher.com/ltc/tx"

STAFF_ROLE_ID = os.getenv("STAFF_ROLE_ID", "").strip()
STAFF_NOTIFY_CHANNEL_ID = os.getenv("STAFF_NOTIFY_CHANNEL_ID", "").strip()


class DealConfirmationView(ui.LayoutView):
    def __init__(
        self,
        deal_id: str = None,
        sender_mention: str = None,
        receiver_mention: str = None,
        amount: float = None,
        usd_amount: float = None,
        asset: str = None,
        txid: str = None,
        created_at: str = None,
    ):
        super().__init__(timeout=None)
        self.deal_id = deal_id
        self.txid = txid

        sender_mention = sender_mention or "**Buyer**"
        receiver_mention = receiver_mention or "**Seller**"
        amount = amount or 0.0
        usd_amount = usd_amount or 0.0
        asset = asset or "LTC"
        txid = txid or "N/A"
        created_at = created_at or datetime.datetime.now(
            datetime.timezone.utc
        ).strftime("%A, %B %d, %Y at %I:%M %p")

        container = ui.Container(accent_color=BRAND_COLOR)

        container.add_item(ui.TextDisplay(
            f"{sender_mention} {receiver_mention}"
        ))
        container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            "## Deal Confirmation\n"
            "**The funds have been successfully secured in our escrow wallet.**"
        ))
        container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            "The transaction has been verified on the blockchain.\n\n"
            f"**Seller ({receiver_mention}):**\n"
            "You may now proceed with the delivery of the item/service.\n\n"
            f"**Buyer ({sender_mention}):**\n"
            "Do **NOT** release funds until you have fully received and verified the item."
        ))
        container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            f"**Amount Secured**\n"
            f"`{amount:.8f}` {asset}\n"
            f"(`~ ${usd_amount} USD`)"
        ))
        container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            f"**Transaction ID**\n"
            f"`{txid}`\n"
            f"[View on Blockchain]({EXPLORER_BASE}/{txid})"
        ))
        container.add_item(ui.Separator())

        row = ui.ActionRow()

        release_btn = ui.Button(
            label="Release",
            style=discord.ButtonStyle.success,
            custom_id="deal_conf_release",
        )
        release_btn.callback = self._on_release
        row.add_item(release_btn)

        cancel_btn = ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id="deal_conf_cancel",
        )
        cancel_btn.callback = self._on_cancel
        row.add_item(cancel_btn)

        info_btn = ui.Button(
            label="More Info",
            style=discord.ButtonStyle.secondary,
            custom_id="deal_conf_info",
        )
        info_btn.callback = self._on_info
        row.add_item(info_btn)

        mod_btn = ui.Button(
            label="Contact Mod",
            style=discord.ButtonStyle.primary,
            custom_id="deal_conf_mod",
        )
        mod_btn.callback = self._on_mod
        row.add_item(mod_btn)

        row.add_item(ui.Button(
            label="View on Blockchain",
            url=f"{EXPLORER_BASE}/{txid}",
            style=discord.ButtonStyle.link,
        ))

        container.add_item(row)
        container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            f"-# Crazy MM & Exch | {created_at}"
        ))

        self.add_item(container)

    async def _on_release(self, interaction: discord.Interaction):
        try:
            from buttons.release import handle_release_button
        except ImportError as e:
            logger.warning(f"buttons.release missing: {e}")
            await interaction.response.send_message(
                "❌ Release system not available. Contact admin.",
                ephemeral=True,
            )
            return

        try:
            await handle_release_button(interaction)
        except Exception as e:
            logger.error(f"Release handler error: {e}", exc_info=True)
            try:
                await interaction.response.send_message(
                    f"❌ Error: {e}",
                    ephemeral=True,
                )
            except Exception:
                pass

    async def _on_cancel(self, interaction: discord.Interaction):
        try:
            from buttons.cancel import handle_cancel_button
        except ImportError as e:
            logger.warning(f"buttons.cancel missing: {e}")
            await interaction.response.send_message(
                "❌ Cancel system not available. Contact admin.",
                ephemeral=True,
            )
            return

        try:
            await handle_cancel_button(interaction)
        except Exception as e:
            logger.error(f"Cancel handler error: {e}", exc_info=True)
            try:
                await interaction.response.send_message(
                    f"❌ Error: {e}",
                    ephemeral=True,
                )
            except Exception:
                pass

    async def _on_info(self, interaction: discord.Interaction):
        from Database import DatabaseManager

        try:
            deal = await DatabaseManager.get_deal_by_channel(interaction.channel_id)
            if not deal:
                await interaction.response.send_message(
                    "❌ Deal not found.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="📋 Deal Details",
                color=BRAND_COLOR,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            embed.add_field(name="Deal ID", value=f"`{deal.deal_id}`", inline=False)
            embed.add_field(name="Status", value=f"`{deal.status}`", inline=True)
            embed.add_field(name="Asset", value=deal.asset, inline=True)
            embed.add_field(name="USD", value=f"`${deal.usd_amount}`", inline=True)
            embed.add_field(
                name="Crypto",
                value=f"`{deal.crypto_amount} {deal.asset}`",
                inline=True,
            )
            embed.add_field(
                name="Sender",
                value=f"<@{deal.sender_id}>" if deal.sender_id else "N/A",
                inline=True,
            )
            embed.add_field(
                name="Receiver",
                value=f"<@{deal.receiver_id}>" if deal.receiver_id else "N/A",
                inline=True,
            )
            embed.add_field(name="TX ID", value=f"`{deal.txid}`", inline=False)
            embed.add_field(
                name="Address",
                value=f"`{deal.deposit_address}`",
                inline=False,
            )
            embed.set_footer(text="Crazy MM & Exchange")

            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.info(f"Info viewed by {interaction.user.id} | Deal: {deal.deal_id}")

        except Exception as e:
            logger.error(f"Info error: {e}", exc_info=True)
            try:
                await interaction.response.send_message(
                    "❌ Error fetching deal info.",
                    ephemeral=True,
                )
            except Exception:
                pass

    async def _on_mod(self, interaction: discord.Interaction):
        from Database import DatabaseManager

        try:
            await interaction.response.defer(ephemeral=True)

            deal = await DatabaseManager.get_deal_by_channel(interaction.channel_id)
            deal_id = deal.deal_id if deal else "unknown"

            role = "User"
            if deal:
                if deal.sender_id == interaction.user.id:
                    role = "Buyer (Sender)"
                elif deal.receiver_id == interaction.user.id:
                    role = "Seller (Receiver)"

            user_embed = discord.Embed(
                title="📞 Mods Notified",
                description=(
                    "Your request has been sent to the staff team.\n"
                    "A moderator will respond in your deal channel shortly.\n\n"
                    "⏳ Please be patient — do not spam."
                ),
                color=BRAND_COLOR,
            )
            user_embed.set_footer(text="Crazy MM & Exchange")
            await interaction.followup.send(embed=user_embed, ephemeral=True)

            if not STAFF_NOTIFY_CHANNEL_ID:
                logger.warning("STAFF_NOTIFY_CHANNEL_ID not set.")
                return

            try:
                notify_channel = interaction.client.get_channel(int(STAFF_NOTIFY_CHANNEL_ID))
            except (ValueError, TypeError):
                notify_channel = None

            if not notify_channel:
                logger.warning(
                    f"Staff notify channel {STAFF_NOTIFY_CHANNEL_ID} not found."
                )
                return

            ping_content = ""
            if STAFF_ROLE_ID:
                ping_content = f"<@&{STAFF_ROLE_ID}>"

            channel_link = f"https://discord.com/channels/{interaction.guild.id}/{interaction.channel_id}"

            staff_embed = discord.Embed(
                title="🚨 Mod Assistance Requested",
                color=discord.Color.orange(),
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            staff_embed.add_field(
                name="Requested by",
                value=f"{interaction.user.mention} (`{interaction.user.id}`)\n**Role:** {role}",
                inline=False,
            )
            staff_embed.add_field(
                name="Deal ID",
                value=f"`{deal_id}`",
                inline=True,
            )
            staff_embed.add_field(
                name="Channel",
                value=f"[Jump to deal]({channel_link})",
                inline=True,
            )
            staff_embed.set_footer(text="Crazy MM & Exchange • Staff Notify")

            await notify_channel.send(
                content=ping_content if ping_content else None,
                embed=staff_embed,
            )

            logger.info(
                f"📞 Mod contacted by {interaction.user.id} | Deal: {deal_id}"
            )

        except Exception as e:
            logger.error(f"Contact mod error: {e}", exc_info=True)
            try:
                await interaction.followup.send(
                    "❌ Error notifying staff.",
                    ephemeral=True,
                )
            except Exception:
                pass


async def send_deal_confirmation(
    bot: discord.Client,
    deal,
    txid: str,
    amount: float,
    confirmations: int,
):
    try:
        channel = bot.get_channel(deal.channel_id)
        if not channel:
            logger.warning(f"Channel {deal.channel_id} not found")
            return False

        sender_mention = f"<@{deal.sender_id}>" if deal.sender_id else "**Buyer**"
        receiver_mention = f"<@{deal.receiver_id}>" if deal.receiver_id else "**Seller**"

        created_at = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%A, %B %d, %Y at %I:%M %p"
        )

        layout = DealConfirmationView(
            deal_id=deal.deal_id,
            sender_mention=sender_mention,
            receiver_mention=receiver_mention,
            amount=amount,
            usd_amount=deal.usd_amount,
            asset=deal.asset,
            txid=txid,
            created_at=created_at,
        )

        target_msg = None
        async for msg in channel.history(limit=100):
            if msg.author != bot.user:
                continue
            if msg.embeds:
                title = (msg.embeds[0].title or "").lower()
                if any(k in title for k in (
                    "payment detected",
                    "waiting for payment",
                    "low amount",
                )):
                    target_msg = msg
                    break

        if target_msg:
            try:
                await target_msg.delete()
                logger.debug(f"Old message deleted: {target_msg.id}")
            except (discord.NotFound, discord.Forbidden) as e:
                logger.warning(f"Old message delete failed: {e}")

        await channel.send(view=layout)
        logger.info(f"Deal Confirmation sent — Deal {deal.deal_id}")
        return True

    except Exception as e:
        logger.error(f"Deal Confirmation error: {e}", exc_info=True)
        return False