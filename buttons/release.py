import discord
from discord import ui
import logging
import hashlib
import asyncio
import os
from pathlib import Path
from typing import Optional, List
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("Release")

BRAND_COLOR = discord.Color(0x0303d4)
EXPLORER_BASE = "https://live.blockcypher.com/ltc/tx"
DEFAULT_FEE_SATS = 10_000
SATOSHIS_PER_LTC = 100_000_000
CHANNEL_DELETE_DELAY_SECONDS = int(os.getenv("CHANNEL_DELETE_DELAY_SECONDS", "60"))
IMMEDIATE_DM_TIMEOUT = 25

BASE_DIR = Path(__file__).resolve().parent.parent
DETAIL_BACKUP_DIR = BASE_DIR / "detail_backup"


class ReleaseConfirmView(ui.LayoutView):
    def __init__(self, deal_id: str = None, sender_id: int = None, receiver_id: int = None, show_buttons: bool = True):
        super().__init__(timeout=None)
        self.deal_id = deal_id
        self.sender_id = sender_id
        self.receiver_id = receiver_id

        container = ui.Container(accent_color=BRAND_COLOR)

        container.add_item(ui.TextDisplay(
            "## Confirm Release\n"
            "**Are you sure?**"
        ))
        container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            "Only confirm if you have **received everything as agreed**.\n\n"
            "⚠️ **This action cannot be undone** once funds are released.\n"
            "The escrow amount will be sent to the seller's provided address."
        ))
        container.add_item(ui.Separator())

        if show_buttons:
            row = ui.ActionRow()

            confirm_btn = ui.Button(
                label="Confirm Release",
                style=discord.ButtonStyle.success,
                custom_id="rel_confirm",
            )
            confirm_btn.callback = self._on_confirm
            row.add_item(confirm_btn)

            back_btn = ui.Button(
                label="Go Back",
                style=discord.ButtonStyle.secondary,
                custom_id="rel_back",
            )
            back_btn.callback = self._on_back
            row.add_item(back_btn)

            container.add_item(row)
            container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            "-# Crazy MM & Exchange | Release Confirmation"
        ))

        self.add_item(container)

    async def _get_deal(self, interaction: discord.Interaction):
        from Database import DatabaseManager
        return await DatabaseManager.get_deal_by_channel(interaction.channel_id)

    async def _on_confirm(self, interaction: discord.Interaction):
        await interaction.response.defer()

        from Database import DatabaseManager
        try:
            deal = await self._get_deal(interaction)
            if not deal:
                await interaction.followup.send("❌ Deal not found.", ephemeral=True)
                return

            if interaction.user.id != deal.sender_id:
                await interaction.followup.send("❌ Only the buyer (sender) can confirm release.", ephemeral=True)
                return

            locked = await DatabaseManager.lock_deal_status(deal.deal_id, "PAYMENT_CONFIRMED", "RELEASE_APPROVED")
            if not locked:
                await interaction.followup.send("❌ Cannot release. Deal is already in progress or has been cancelled.", ephemeral=True)
                return

            await DatabaseManager.add_audit_log(deal.deal_id, "RELEASE_CONFIRMED_BY_BUYER", interaction.user.id)

            no_btn_view = ReleaseConfirmView(
                deal_id=deal.deal_id,
                sender_id=deal.sender_id,
                receiver_id=deal.receiver_id,
                show_buttons=False,
            )
            try:
                await interaction.message.edit(view=no_btn_view)
            except Exception as e:
                logger.warning(f"ReleaseConfirm hide buttons failed: {e}")

            await interaction.channel.send(
                view=EnterAddressView(
                    deal_id=deal.deal_id,
                    sender_id=deal.sender_id,
                    receiver_id=deal.receiver_id,
                )
            )
            logger.info(f"Release confirmed by buyer {interaction.user.id} | Deal: {deal.deal_id}")

        except Exception as e:
            logger.error(f"Release confirm error: {e}", exc_info=True)
            try:
                await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass

    async def _on_back(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.message.delete()
        except Exception:
            pass
        await interaction.followup.send("↩️ Release cancelled.", ephemeral=True)


class EnterAddressView(ui.LayoutView):
    def __init__(self, deal_id: str = None, sender_id: int = None, receiver_id: int = None, show_buttons: bool = True):
        super().__init__(timeout=None)
        self.deal_id = deal_id
        self.sender_id = sender_id
        self.receiver_id = receiver_id

        container = ui.Container(accent_color=BRAND_COLOR)

        container.add_item(ui.TextDisplay(
            "## ✅ Funds Ready to Release\n"
            "**The buyer has confirmed the deal.**"
        ))
        container.add_item(ui.Separator())

        seller_mention = f"<@{receiver_id}>" if receiver_id else "**Seller**"

        container.add_item(ui.TextDisplay(
            f"{seller_mention}, please enter your **Litecoin address** to receive the funds.\n\n"
            "**Supported address types:**\n"
            "▸ Legacy (`L...`)\n"
            "▸ P2SH (`M...`)\n"
            "▸ Native SegWit (`ltc1q...`)\n"
            "▸ Taproot (`ltc1p...`)\n\n"
            "⚠️ **MWEB addresses are not supported.**"
        ))
        container.add_item(ui.Separator())

        if show_buttons:
            row = ui.ActionRow()
            enter_btn = ui.Button(
                label="Enter Address",
                style=discord.ButtonStyle.success,
                custom_id="rel_enter_addr",
            )
            enter_btn.callback = self._on_enter
            row.add_item(enter_btn)
            container.add_item(row)
            container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            "-# Crazy MM & Exchange | Waiting for seller address"
        ))

        self.add_item(container)

    async def _get_deal(self, interaction: discord.Interaction):
        from Database import DatabaseManager
        return await DatabaseManager.get_deal_by_channel(interaction.channel_id)

    async def _on_enter(self, interaction: discord.Interaction):
        deal = await self._get_deal(interaction)
        if not deal:
            await interaction.response.send_message("❌ Deal not found.", ephemeral=True)
            return

        if interaction.user.id != deal.receiver_id:
            await interaction.response.send_message("❌ Only the seller can enter the address.", ephemeral=True)
            return

        if deal.status != "RELEASE_APPROVED":
            await interaction.response.send_message(f"❌ Cannot submit address now (status: `{deal.status}`).", ephemeral=True)
            return

        await interaction.response.send_modal(AddressModal(deal_id=deal.deal_id, role="seller"))


class AddressModal(ui.Modal, title="Enter LTC Address"):
    address_input = ui.TextInput(
        label="Your Litecoin Address",
        placeholder="ltc1... or L...",
        required=True,
        max_length=120,
        custom_id="recipient_ltc_address",
    )

    def __init__(self, deal_id: str, role: str):
        super().__init__()
        self.deal_id = deal_id
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        from send import validate_recipient_address
        from Database import DatabaseManager

        await interaction.response.defer(ephemeral=True)

        address = self.address_input.value.strip()

        ok, err_msg = validate_recipient_address(address)
        if not ok:
            await interaction.followup.send(err_msg, ephemeral=True)
            return

        deal = await DatabaseManager.get_deal_by_id(self.deal_id)
        if not deal:
            await interaction.followup.send("❌ Deal not found.", ephemeral=True)
            return

        if interaction.user.id != deal.receiver_id:
            await interaction.followup.send("❌ Only the seller can submit the address.", ephemeral=True)
            return

        if deal.status != "RELEASE_APPROVED":
            await interaction.followup.send(f"❌ Deal is no longer in release state (status: `{deal.status}`).", ephemeral=True)
            return

        confirm_embed = discord.Embed(
            title="⚠️ Confirm Address Before Sending",
            description="Please verify the address below. Funds will be **irreversibly** sent to this address.",
            color=discord.Color.orange(),
        )
        confirm_embed.add_field(name="Recipient Address", value=f"`{address}`", inline=False)
        confirm_embed.add_field(name="Amount", value=f"`{deal.crypto_amount} {deal.asset}` (~${deal.usd_amount} USD)", inline=False)
        confirm_embed.add_field(name="Network", value=f"`{deal.asset}` mainnet", inline=False)
        confirm_embed.set_footer(text="Crazy MM & Exchange | Double-check before confirming")

        await interaction.followup.send(
            embed=confirm_embed,
            view=AddressConfirmView(
                deal_id=deal.deal_id,
                address=address,
                receiver_id=deal.receiver_id,
                role="seller",
            ),
            ephemeral=True,
        )


class AddressConfirmView(ui.View):
    def __init__(self, deal_id: str, address: str, receiver_id: int, role: str = "seller"):
        super().__init__(timeout=300)
        self.deal_id = deal_id
        self.address = address
        self.receiver_id = receiver_id
        self.role = role

    @discord.ui.button(label="Confirm & Send", style=discord.ButtonStyle.success, custom_id="addr_confirm_send")
    async def _on_confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.receiver_id:
            await interaction.response.send_message("❌ This confirmation is not for you.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        from send import send_ltc
        from Database import DatabaseManager
        from wallet import WalletManager

        deal = await DatabaseManager.get_deal_by_id(self.deal_id)
        if not deal:
            await interaction.followup.send("❌ Deal not found.", ephemeral=True)
            return

        if deal.status != "RELEASE_APPROVED":
            await interaction.followup.send(f"❌ Deal is no longer in release state (status: `{deal.status}`).", ephemeral=True)
            return

        locked = await DatabaseManager.lock_deal_status(deal.deal_id, "RELEASE_APPROVED", "RELEASING")
        if not locked:
            await interaction.followup.send("❌ Cannot process. Deal status changed.", ephemeral=True)
            return

        await interaction.followup.send("⏳ Broadcasting transaction... Please wait.", ephemeral=True)

        wallet = WalletManager()
        deal_index = int(hashlib.sha256(deal.deal_id.encode()).hexdigest()[:8], 16) % 1000000

        try:
            priv_hex = wallet.get_private_key(
                coin_symbol=deal.asset,
                address_type=deal.address_type or "legacy",
                index=deal_index,
            )
        except Exception as e:
            logger.error(f"Private key derivation failed: {e}", exc_info=True)
            await DatabaseManager.update_status(deal.deal_id, "RELEASE_FAILED")
            await interaction.followup.send(f"❌ Failed to derive private key: {e}", ephemeral=True)
            return

        try:
            result = await asyncio.to_thread(send_ltc, priv_hex, self.address, deal.crypto_amount, DEFAULT_FEE_SATS)
        except Exception as e:
            logger.error(f"Send failed: {e}", exc_info=True)
            await DatabaseManager.update_status(deal.deal_id, "RELEASE_FAILED")
            await interaction.followup.send(f"❌ Send failed: {e}", ephemeral=True)
            return

        if not result.get("success"):
            await DatabaseManager.update_status(deal.deal_id, "RELEASE_FAILED")
            await DatabaseManager.add_audit_log(deal.deal_id, "RELEASE_FAILED", interaction.user.id, f"Error: {result.get('error')}")
            await interaction.followup.send(f"❌ Transaction failed: {result.get('error', 'Unknown error')}", ephemeral=True)
            return

        txid = result["txid"]
        send_sats = result["send_sats"]
        fee_sats = result["fee_sats"]
        send_amount = send_sats / SATOSHIS_PER_LTC
        fee_amount = fee_sats / SATOSHIS_PER_LTC

        await DatabaseManager.update_status(deal.deal_id, "COMPLETED")
        await DatabaseManager.add_audit_log(
            deal.deal_id,
            "FUNDS_RELEASED",
            interaction.user.id,
            f"TX: {txid} | Amount: {send_amount:.8f} {deal.asset} | Fee: {fee_amount:.8f}",
        )

        success_embed = discord.Embed(
            title="✅ Funds Released Successfully",
            description=(
                f"Funds have been sent to the seller.\n\n"
                f"**Transaction ID:** `{txid}`\n"
                f"[View on Blockchain]({EXPLORER_BASE}/{txid})"
            ),
            color=discord.Color.green(),
        )
        success_embed.add_field(name="Amount Sent", value=f"`{send_amount:.8f} {deal.asset}`", inline=True)
        success_embed.add_field(name="Network Fee", value=f"`{fee_amount:.8f} {deal.asset}`", inline=True)
        success_embed.add_field(name="Recipient", value=f"`{self.address}`", inline=False)
        success_embed.set_footer(text="Crazy MM & Exchange")

        try:
            await interaction.channel.send(embed=success_embed)
        except Exception as e:
            logger.error(f"Success embed send failed: {e}")

        await interaction.followup.send("✅ Funds released!", ephemeral=True)

        logger.info(f"Funds released | Deal: {deal.deal_id} | TX: {txid} | Amount: {send_amount:.8f} {deal.asset}")

        try:
            no_btn_view = EnterAddressView(
                deal_id=deal.deal_id,
                sender_id=deal.sender_id,
                receiver_id=deal.receiver_id,
                show_buttons=False,
            )
            await interaction.channel.send(view=no_btn_view)
        except Exception:
            pass

        failed_users = await _hybrid_dm_delivery(
            bot=interaction.client,
            deal=deal,
            txid=txid,
            send_amount=send_amount,
            fee_amount=fee_amount,
            recipient=self.address,
            kind="RELEASE",
        )

        if failed_users:
            await DatabaseManager.enqueue_task(
                "post_transaction_dm",
                {
                    "deal_id": deal.deal_id,
                    "txid": txid,
                    "send_amount": send_amount,
                    "fee_amount": fee_amount,
                    "recipient": self.address,
                    "kind": "RELEASE",
                    "only_for": failed_users,
                },
            )
            logger.info(f"DM fallback queued for users: {failed_users} | Deal: {deal.deal_id}")

        await DatabaseManager.schedule_channel_delete(deal.deal_id, CHANNEL_DELETE_DELAY_SECONDS)
        logger.info(f"Channel delete scheduled ({CHANNEL_DELETE_DELAY_SECONDS}s) for deal {deal.deal_id}")

    @discord.ui.button(label="Change Address", style=discord.ButtonStyle.secondary, custom_id="addr_change")
    async def _on_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.receiver_id:
            await interaction.response.send_message("❌ This action is not for you.", ephemeral=True)
            return
        await interaction.response.send_modal(AddressModal(deal_id=self.deal_id, role="seller"))


async def _hybrid_dm_delivery(
    bot: discord.Client,
    deal,
    txid: str,
    send_amount: float,
    fee_amount: float,
    recipient: str,
    kind: str,
) -> List[int]:
    try:
        from backup_manager.transcript_manager import finalize_transcript
    except ImportError:
        finalize_transcript = None

    transcript_path = None
    if finalize_transcript:
        try:
            transcript_path = await finalize_transcript(bot, deal)
            if transcript_path:
                logger.info(f"Transcript finalized: {transcript_path}")
        except Exception as e:
            logger.error(f"Transcript finalization failed: {e}")

    try:
        from backup_manager.backup_manager import get_backup_manager
        bm = get_backup_manager()
        await bm.update_extended_backup(
            deal.deal_id,
            {
                "status": "COMPLETED",
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
        asyncio.create_task(bm.upload_extended_to_clouds(deal.deal_id))
    except Exception as e:
        logger.error(f"Extended backup update failed: {e}")

    failed_users: List[int] = []

    for uid, role_label in [(deal.sender_id, "Buyer"), (deal.receiver_id, "Seller")]:
        if not uid:
            continue

        try:
            success = await asyncio.wait_for(
                _send_dm_to_user(
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
                ),
                timeout=IMMEDIATE_DM_TIMEOUT,
            )
            if not success:
                failed_users.append(uid)
                logger.warning(f"DM delivery failed for {uid} — will retry via queue")
        except asyncio.TimeoutError:
            logger.warning(f"DM to {uid} timed out — will retry via queue")
            failed_users.append(uid)
        except Exception as e:
            logger.error(f"DM to {uid} failed: {e} — will retry via queue")
            failed_users.append(uid)

    return failed_users


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
) -> bool:
    if not user_id:
        return True

    try:
        user = await bot.fetch_user(user_id)
    except Exception as e:
        logger.warning(f"Could not fetch user {user_id}: {e}")
        return False

    try:
        title = "✅ Deal Completed — Funds Released"
        description = (
            f"Your escrow deal has been successfully completed.\n"
            f"Funds have been released to the seller.\n\n"
            f"**Deal Details**"
        )

        embed = discord.Embed(title=title, description=description, color=discord.Color.green())
        embed.add_field(name="Deal ID", value=f"`{deal.deal_id}`", inline=False)
        embed.add_field(name="Your Role", value=role_label, inline=True)
        embed.add_field(name="Asset", value=deal.asset, inline=True)
        embed.add_field(name="Amount Sent", value=f"`{send_amount:.8f} {deal.asset}`", inline=True)
        embed.add_field(name="Network Fee", value=f"`{fee_amount:.8f} {deal.asset}`", inline=True)
        embed.add_field(name="Recipient", value=f"`{recipient}`", inline=False)
        embed.add_field(
            name="Transaction ID",
            value=f"`{txid}`\n[View on Blockchain]({EXPLORER_BASE}/{txid})",
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
                        value=f"⚠️ Transcript too large ({size_mb:.2f} MB) — request from admin.",
                        inline=False,
                    )

        await user.send(embed=embed, files=files if files else [])
        logger.info(f"DM sent to {user_id} for deal {deal.deal_id}")
        return True

    except discord.Forbidden:
        logger.warning(f"Cannot DM user {user_id} (DMs blocked).")
        return False
    except Exception as e:
        logger.error(f"DM failed for user {user_id}: {e}")
        return False


async def _post_transaction_tasks(
    bot: discord.Client,
    deal,
    txid: str,
    send_amount: float,
    fee_amount: float,
    recipient: str,
    kind: str,
):
    await _hybrid_dm_delivery(bot, deal, txid, send_amount, fee_amount, recipient, kind)


async def handle_release_button(interaction: discord.Interaction):
    from Database import DatabaseManager

    try:
        await interaction.response.defer(ephemeral=True)

        deal = await DatabaseManager.get_deal_by_channel(interaction.channel_id)
        if not deal:
            await interaction.followup.send("❌ Deal not found.", ephemeral=True)
            return

        if deal.status != "PAYMENT_CONFIRMED":
            await interaction.followup.send(f"❌ Release not available (status: `{deal.status}`).", ephemeral=True)
            return

        if interaction.user.id == deal.receiver_id:
            await interaction.followup.send("❌ **Only the sender can release.**", ephemeral=True)
            return

        if interaction.user.id != deal.sender_id:
            await interaction.followup.send("❌ You are not part of this deal.", ephemeral=True)
            return

        await interaction.channel.send(
            view=ReleaseConfirmView(
                deal_id=deal.deal_id,
                sender_id=deal.sender_id,
                receiver_id=deal.receiver_id,
            )
        )
        await interaction.followup.send("⚠️ Please confirm the release in the message above.", ephemeral=True)
        logger.info(f"Release initiated by buyer {interaction.user.id} | Deal: {deal.deal_id}")

    except Exception as e:
        logger.error(f"handle_release_button error: {e}", exc_info=True)
        try:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
        except Exception:
            pass


async def force_release_deal(interaction: discord.Interaction, deal_id: Optional[str] = None):
    from Database import DatabaseManager

    try:
        if deal_id:
            deal = await DatabaseManager.get_deal_by_id(deal_id.strip())
            if not deal:
                await interaction.followup.send(f"❌ Deal `{deal_id}` not found.", ephemeral=True)
                return
        else:
            deal = await DatabaseManager.get_deal_by_channel(interaction.channel_id)
            if not deal:
                await interaction.followup.send("❌ This is not an AutoMM channel and no `deal_id` was provided.", ephemeral=True)
                return

        if deal.status in ("COMPLETED", "CANCELLED", "REFUNDED", "RELEASING", "REFUNDING"):
            await interaction.followup.send(f"❌ Deal is already `{deal.status}`. Cannot force-release.", ephemeral=True)
            return

        target_channel = interaction.client.get_channel(deal.channel_id)
        if not target_channel:
            await interaction.followup.send("❌ Deal channel not found.", ephemeral=True)
            return

        current_status = deal.status
        locked = await DatabaseManager.lock_deal_status(deal.deal_id, current_status, "RELEASE_APPROVED")
        if not locked:
            await interaction.followup.send("❌ Could not lock deal status. Another action may be in progress.", ephemeral=True)
            return

        await DatabaseManager.add_audit_log(deal.deal_id, "FORCE_RELEASE_BY_ADMIN", interaction.user.id, f"{current_status} -> RELEASE_APPROVED")

        await target_channel.send(
            view=EnterAddressView(
                deal_id=deal.deal_id,
                sender_id=deal.sender_id,
                receiver_id=deal.receiver_id,
            )
        )

        logger.info(f"Force-release initiated by admin {interaction.user.id} | Deal: {deal.deal_id}")
        await interaction.followup.send(
            f"✅ Force-release initiated for `{deal.deal_id}`.\n"
            f"Seller has been asked to provide their address in {target_channel.mention}.",
            ephemeral=True,
        )

    except Exception as e:
        logger.error(f"force_release_deal error: {e}", exc_info=True)
        try:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
        except Exception:
            pass