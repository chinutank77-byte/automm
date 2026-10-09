import discord
import logging
import aiohttp
import uuid
import asyncio
import hashlib
import os
import json
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("RoleSelection")

GUIDELINES_CHANNEL = "<#CHANNEL_ID>"
TOS_CHANNEL = "<#CHANNEL_ID>"
AUTOKMM_TOS_CHANNEL = "<#CHANNEL_ID>"

BRAND_COLOR = discord.Color(0x0303d4)

DEAL_RATE_LIMIT_PER_HOUR = int(os.getenv("DEAL_RATE_LIMIT_PER_HOUR", "5"))
DEAL_RATE_LIMIT_PER_DAY = int(os.getenv("DEAL_RATE_LIMIT_PER_DAY", "12"))
MAX_CONCURRENT_DEALS = int(os.getenv("MAX_CONCURRENT_DEALS", "14"))


class SetupMMPanel(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)

        container = discord.ui.Container(accent_color=BRAND_COLOR)

        container.add_item(discord.ui.TextDisplay(
            "# 🛡️ Crazy MM & Exchange\n"
            "### Secure Cryptocurrency Escrow Services"
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            "We provide a **protected environment** for your cryptocurrency trades, "
            "helping prevent fraud and scams on every deal.\n\n"
            "**Why choose us?**\n"
            "▸ 🛡️ **Escrow-protected** — funds safe until deal completes\n"
            "▸ 🔒 **Auto-verified** — blockchain confirmation in 15 seconds\n"
            "▸ ⚖️ **Dispute support** — admin intervention if issues arise"
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            "### 📜 Read Before Proceeding\n"
            f"**Guidelines** → {GUIDELINES_CHANNEL}\n"
            f"**Terms of Service** → {TOS_CHANNEL}\n"
            f"**AutoMM TOS** → {AUTOKMM_TOS_CHANNEL}"
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            "### 💰 Select an asset below to begin your transaction"
        ))

        row = discord.ui.ActionRow()
        asset_select = discord.ui.Select(
            custom_id="asset_select",
            placeholder="Choose an asset to begin",
            options=[
                discord.SelectOption(label="Litecoin (LTC)", value="LTC", emoji="🪙", description="Fast & low fees"),
                discord.SelectOption(label="Bitcoin (BTC)", value="BTC", emoji="🟠", description="Coming soon"),
                discord.SelectOption(label="Ethereum (ETH)", value="ETH", emoji="💎", description="Coming soon"),
            ],
        )
        asset_select.callback = self._on_asset_select
        row.add_item(asset_select)

        container.add_item(row)
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            "-# Crazy MM & Exch • Powered by AutoMM"
        ))

        self.add_item(container)

    async def _on_asset_select(self, interaction: discord.Interaction):
        try:
            selected = interaction.data["values"][0]
        except (KeyError, IndexError):
            await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)
            return

        if selected != "LTC":
            await interaction.response.send_message(
                f"⚠️ **{selected}** is not supported yet. Only **Litecoin (LTC)** is available.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(DealModal(asset=selected))


class AssetSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label="Litecoin (LTC)", value="LTC")]
        super().__init__(placeholder="Choose an asset to begin", options=options, custom_id="asset_select_legacy")

    async def callback(self, interaction: discord.Interaction):
        selected_asset = self.values[0]
        if selected_asset != "LTC":
            await interaction.response.send_message(
                f"⚠️ **{selected_asset}** is not supported yet.", ephemeral=True
            )
            return
        await interaction.response.send_modal(DealModal(asset=selected_asset))


class AssetView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(AssetSelect())


class DealModal(discord.ui.Modal, title="Deal Info"):
    user_id_input = discord.ui.TextInput(
        label="Enter Seller/Buyer User ID or Username",
        placeholder="User ID, @mention, or exact username...",
        required=True,
        max_length=100,
        custom_id="target_user_input",
    )

    def __init__(self, asset: str):
        super().__init__()
        self.asset = asset

    async def on_submit(self, interaction: discord.Interaction):
        from Database import DatabaseManager

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        target_user = await self._resolve_user(interaction, self.user_id_input.value)
        if not target_user:
            await interaction.followup.send("❌ Invalid user ID or user not found.", ephemeral=True)
            return

        if target_user.id == interaction.user.id:
            await interaction.followup.send("❌ You cannot deal with yourself.", ephemeral=True)
            return

        if target_user.bot:
            await interaction.followup.send("❌ You cannot deal with a bot.", ephemeral=True)
            return

        allowed, err_msg = await DatabaseManager.check_deal_create_limit(
            user_id=interaction.user.id,
            per_hour=DEAL_RATE_LIMIT_PER_HOUR,
            per_day=DEAL_RATE_LIMIT_PER_DAY,
            max_active=MAX_CONCURRENT_DEALS,
        )

        if not allowed:
            await interaction.followup.send(err_msg, ephemeral=True)
            logger.warning(f"Deal create blocked for {interaction.user.id}: {err_msg}")
            return

        deal_id = f"CRAZY-{uuid.uuid4().hex[:20].upper()}"

        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
                interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                target_user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
            }
            channel = await guild.create_text_channel(
                name=f"auto-{deal_id[-5:]}",
                overwrites=overwrites,
                topic=f"Escrow Deal: {deal_id} | Asset: {self.asset}",
            )
        except discord.Forbidden:
            await interaction.followup.send("❌ Missing permission to create channel.", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"Channel creation failed: {e}")
            await interaction.followup.send("❌ Channel creation failed.", ephemeral=True)
            return

        success = await DatabaseManager.create_deal(
            deal_id=deal_id,
            channel_id=channel.id,
            guild_id=guild.id,
            asset=self.asset,
            address_type="legacy",
        )

        if not success:
            await channel.delete()
            await interaction.followup.send("❌ Failed to save deal in database.", ephemeral=True)
            return

        welcome_embed = discord.Embed(
            title="Welcome to Crazy Escrow",
            description="This ticket is for both of you — please only follow messages sent by the bot.",
            color=BRAND_COLOR,
        )
        welcome_embed.add_field(
            name="🔹 Step One — Set Up the Deal",
            value=(
                "• Choose your role: **Sender** or **Receiver**\n"
                "• Talk it through — agree on the product, price, and terms\n"
                "• Double-check the final amount and payment network together\n"
                "• Wait for the bot's invoice, that's your signal to pay, not before"
            ),
            inline=False,
        )
        welcome_embed.add_field(
            name="🔒 Step Two — Stay Protected",
            value=(
                "• Don't pay through DMs\n"
                "• The bot is the only source for payment details\n"
                "• Something feel off? Ping staff before you send anything"
            ),
            inline=False,
        )
        welcome_embed.add_field(
            name="👑 Step Three — Save your Deal ID",
            value=f"`{deal_id}`\nKeep this safe — it's your key to recover this deal anytime",
            inline=False,
        )
        welcome_embed.add_field(
            name="➡️ Ready? Select your role below to start.",
            value="Need another language or a walkthrough? Use the menus above.",
            inline=False,
        )
        welcome_embed.set_thumbnail(url="https://cryptologos.cc/logos/litecoin-ltc-logo.png")
        welcome_embed.set_footer(text="Crazy MM & Exchange")

        await channel.send(
            content=f"{interaction.user.mention} {target_user.mention}",
            embed=welcome_embed,
            view=RoleView(deal_id=deal_id),
        )

        await interaction.followup.send(
            f"✅ Deal created successfully! Head over to {channel.mention}",
            ephemeral=True,
        )
        logger.info(f"Deal {deal_id} created by {interaction.user.id}")

    async def _resolve_user(self, interaction: discord.Interaction, input_str: str):
        input_str = input_str.strip()
        try:
            if input_str.startswith("<@") and input_str.endswith(">"):
                user_id = int(input_str.strip("<@!>"))
                return await interaction.client.fetch_user(user_id)
            if input_str.isdigit():
                return await interaction.client.fetch_user(int(input_str))
            member = discord.utils.get(interaction.guild.members, name=input_str)
            if member:
                return member
            member = discord.utils.get(interaction.guild.members, display_name=input_str)
            if member:
                return member
            return None
        except discord.NotFound:
            return None
        except Exception as e:
            logger.error(f"User resolve error: {e}")
            return None


class RoleView(discord.ui.View):
    def __init__(self, deal_id: str = None):
        super().__init__(timeout=None)
        self.deal_id = deal_id

    async def _get_deal(self, interaction: discord.Interaction):
        from Database import DatabaseManager
        return await DatabaseManager.get_deal_by_channel(interaction.channel_id)

    def _build_embed(self, deal):
        sender_display = f"<@{deal.sender_id}>" if deal.sender_id else "None"
        receiver_display = f"<@{deal.receiver_id}>" if deal.receiver_id else "None"

        embed = discord.Embed(
            title="Select Your Role",
            description="Select your role. Both users must click a button to proceed.",
            color=BRAND_COLOR,
        )
        embed.add_field(name=f"Sending {deal.asset}", value=sender_display, inline=False)
        embed.add_field(name=f"Receiving {deal.asset}", value=receiver_display, inline=False)
        embed.set_thumbnail(url="https://cryptologos.cc/logos/litecoin-ltc-logo.png")
        embed.set_footer(text="Crazy MM & Exchange | Both roles must be selected")
        return embed

    @discord.ui.button(label="I am sending", style=discord.ButtonStyle.primary, custom_id="btn_role_sender")
    async def btn_sender(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        from Database import DatabaseManager
        try:
            deal = await self._get_deal(interaction)
            if not deal:
                await interaction.followup.send("❌ Deal not found.", ephemeral=True)
                return

            if deal.receiver_id == interaction.user.id:
                await interaction.followup.send("❌ You are already the Receiver.", ephemeral=True)
                return

            await DatabaseManager.update_roles(deal_id=deal.deal_id, sender_id=interaction.user.id)
            deal = await self._get_deal(interaction)

            embed = self._build_embed(deal)

            if deal.sender_id and deal.receiver_id:
                try:
                    await interaction.edit_original_response(embed=embed, view=None)
                except discord.NotFound:
                    pass

                await interaction.channel.send(
                    view=UserCheckView(
                        deal_id=deal.deal_id,
                        sender_id=deal.sender_id,
                        receiver_id=deal.receiver_id,
                    )
                )
            else:
                try:
                    await interaction.edit_original_response(embed=embed, view=self)
                except discord.NotFound:
                    pass
        except Exception as e:
            logger.error(f"btn_sender error: {e}", exc_info=True)
            try:
                await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="I am receiving", style=discord.ButtonStyle.success, custom_id="btn_role_receiver")
    async def btn_receiver(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        from Database import DatabaseManager
        try:
            deal = await self._get_deal(interaction)
            if not deal:
                await interaction.followup.send("❌ Deal not found.", ephemeral=True)
                return

            if deal.sender_id == interaction.user.id:
                await interaction.followup.send("❌ You are already the Sender.", ephemeral=True)
                return

            await DatabaseManager.update_roles(deal_id=deal.deal_id, receiver_id=interaction.user.id)
            deal = await self._get_deal(interaction)

            embed = self._build_embed(deal)

            if deal.sender_id and deal.receiver_id:
                try:
                    await interaction.edit_original_response(embed=embed, view=None)
                except discord.NotFound:
                    pass

                await interaction.channel.send(
                    view=UserCheckView(
                        deal_id=deal.deal_id,
                        sender_id=deal.sender_id,
                        receiver_id=deal.receiver_id,
                    )
                )
            else:
                try:
                    await interaction.edit_original_response(embed=embed, view=self)
                except discord.NotFound:
                    pass
        except Exception as e:
            logger.error(f"btn_receiver error: {e}", exc_info=True)
            try:
                await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.danger, custom_id="btn_role_reset")
    async def btn_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        from Database import DatabaseManager
        try:
            deal = await self._get_deal(interaction)
            if not deal:
                await interaction.followup.send("❌ Deal not found.", ephemeral=True)
                return

            await DatabaseManager.reset_roles(deal.deal_id)
            deal = await self._get_deal(interaction)
            embed = self._build_embed(deal)
            try:
                await interaction.edit_original_response(embed=embed, view=self)
            except discord.NotFound:
                pass
        except Exception as e:
            logger.error(f"btn_reset error: {e}")


class UserCheckView(discord.ui.LayoutView):
    def __init__(self, deal_id: str = None, sender_id: int = None, receiver_id: int = None, show_buttons: bool = True):
        super().__init__(timeout=None)
        self.deal_id = deal_id
        self.sender_id = sender_id
        self.receiver_id = receiver_id

        sender_line = f"<@{sender_id}>(`{sender_id}`)" if sender_id else "`N/A`"
        receiver_line = f"<@{receiver_id}>(`{receiver_id}`)" if receiver_id else "`N/A`"

        container = discord.ui.Container(accent_color=BRAND_COLOR)

        container.add_item(discord.ui.TextDisplay(
            "## User Check\n"
            "**Confirm your roles:**"
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            f"**Sender**\n{sender_line}\n\n"
            f"**Receiver**\n{receiver_line}\n\n"
            f"⚠️ Make sure you pick the right role to avoid issues."
        ))
        container.add_item(discord.ui.Separator())

        if show_buttons:
            row = discord.ui.ActionRow()

            correct_btn = discord.ui.Button(
                label="Correct",
                style=discord.ButtonStyle.success,
                custom_id="btn_usercheck_correct",
            )
            correct_btn.callback = self._on_correct
            row.add_item(correct_btn)

            back_btn = discord.ui.Button(
                label="Back",
                style=discord.ButtonStyle.danger,
                custom_id="btn_usercheck_back",
            )
            back_btn.callback = self._on_back
            row.add_item(back_btn)

            container.add_item(row)
            container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            "-# Crazy MM & Exchange | Both users must confirm"
        ))

        self.add_item(container)

    async def _get_deal(self, interaction: discord.Interaction):
        from Database import DatabaseManager
        return await DatabaseManager.get_deal_by_channel(interaction.channel_id)

    async def _on_correct(self, interaction: discord.Interaction):
        from Database import DatabaseManager

        deal = await self._get_deal(interaction)
        if not deal:
            await interaction.response.send_message("❌ Deal not found.", ephemeral=True)
            return

        clicked_count = await DatabaseManager.add_user_check_confirmation(
            deal.deal_id, interaction.user.id
        )

        if clicked_count >= 2:
            await interaction.response.send_modal(AmountModal(deal_id=deal.deal_id))

            try:
                no_buttons_view = UserCheckView(
                    deal_id=deal.deal_id,
                    sender_id=deal.sender_id,
                    receiver_id=deal.receiver_id,
                    show_buttons=False,
                )
                await interaction.message.edit(view=no_buttons_view)
            except Exception as e:
                logger.warning(f"Button remove failed: {e}")
        else:
            await interaction.response.send_message(
                f"✅ Confirmed! Waiting for the other user... ({clicked_count}/2)",
                ephemeral=True,
            )

    async def _on_back(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        from Database import DatabaseManager

        deal = await self._get_deal(interaction)
        if not deal:
            await interaction.followup.send("❌ Deal not found.", ephemeral=True)
            return

        await DatabaseManager.reset_roles(deal.deal_id)

        await interaction.followup.send("↩️ Going back. Please re-select your roles.", ephemeral=True)

        embed = discord.Embed(
            title="Select Your Role",
            description="Role selection reset. Please choose again.",
            color=discord.Color.orange(),
        )
        embed.add_field(
            name=f"Sending {deal.asset}",
            value=f"<@{deal.sender_id}>" if deal.sender_id else "None",
            inline=False,
        )
        embed.add_field(
            name=f"Receiving {deal.asset}",
            value=f"<@{deal.receiver_id}>" if deal.receiver_id else "None",
            inline=False,
        )
        await interaction.channel.send(embed=embed, view=RoleView(deal_id=deal.deal_id))


class AmountModal(discord.ui.Modal, title="Deal Price"):
    usd_amount = discord.ui.TextInput(
        label="Amount in USD",
        placeholder="e.g., 0.10",
        required=True,
        max_length=20,
        custom_id="usd_amount_input",
    )

    def __init__(self, deal_id: str):
        super().__init__()
        self.deal_id = deal_id

    async def _fetch_ltc_price(self) -> float:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.binance.com/api/v3/ticker/price?symbol=LTCUSDT"
                ) as resp:
                    data = await resp.json()
                    return float(data["price"])
        except Exception:
            return 71.62

    async def on_submit(self, interaction: discord.Interaction):
        from Database import DatabaseManager

        await interaction.response.defer()

        try:
            usd = float(self.usd_amount.value)
            if usd <= 0:
                raise ValueError()
            if usd < 0.01:
                await interaction.followup.send("❌ Minimum amount is $0.01 USD.", ephemeral=True)
                return
        except ValueError:
            await interaction.followup.send("❌ Invalid amount! Please enter numbers only.", ephemeral=True)
            return

        ltc_price = await self._fetch_ltc_price()
        crypto_amount = round(usd / ltc_price, 8)

        await DatabaseManager.update_amount(self.deal_id, usd, crypto_amount)
        deal = await DatabaseManager.get_deal_by_id(self.deal_id)

        await interaction.channel.send(
            view=ConfirmAmountView(
                deal_id=deal.deal_id,
                usd=usd,
                crypto=crypto_amount,
                asset=deal.asset,
                ltc_price=ltc_price,
            )
        )


class ConfirmAmountView(discord.ui.LayoutView):
    def __init__(self, deal_id: str = None, usd: float = None, crypto: float = None,
                 asset: str = None, ltc_price: float = None, show_buttons: bool = True):
        super().__init__(timeout=None)
        self.deal_id = deal_id
        self.usd = usd
        self.crypto = crypto

        asset_text = asset or "LTC"
        usd_text = f"`${usd}`" if usd is not None else "`N/A`"
        rate_text = f"1 {asset_text} = ${ltc_price}" if ltc_price is not None else f"1 {asset_text} = N/A"

        container = discord.ui.Container(accent_color=BRAND_COLOR)

        container.add_item(discord.ui.TextDisplay(
            f"## Confirm Amount\n"
            f"Do you confirm the amount is **${usd if usd is not None else 'N/A'}** in {asset_text}?"
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            f"**USD Amount**\n{usd_text}\n\n"
            f"**Payment Network**\n{asset_text}\n\n"
            f"**Reference Rate**\n{rate_text}"
        ))
        container.add_item(discord.ui.Separator())

        if show_buttons:
            row = discord.ui.ActionRow()

            agree_btn = discord.ui.Button(
                label="I Agree",
                style=discord.ButtonStyle.success,
                custom_id="btn_confirm_agree",
            )
            agree_btn.callback = self._on_agree
            row.add_item(agree_btn)

            cancel_btn = discord.ui.Button(
                label="Cancel",
                style=discord.ButtonStyle.danger,
                custom_id="btn_confirm_cancel",
            )
            cancel_btn.callback = self._on_cancel
            row.add_item(cancel_btn)

            container.add_item(row)
            container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            "-# Crazy MM & Exchange | Both must agree"
        ))

        self.add_item(container)

    async def _get_deal(self, interaction: discord.Interaction):
        from Database import DatabaseManager
        return await DatabaseManager.get_deal_by_channel(interaction.channel_id)

    async def _on_agree(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        from Database import DatabaseManager
        from Invoice import send_payment_invoice
        from wallet import WalletManager
        from backup_manager import get_backup_manager

        try:
            deal = await self._get_deal(interaction)
            if not deal:
                await interaction.followup.send("❌ Deal not found.", ephemeral=True)
                return

            clicked = await DatabaseManager.add_amount_agree_confirmation(
                deal.deal_id, interaction.user.id
            )

            if clicked >= 2:
                try:
                    no_buttons_view = ConfirmAmountView(
                        deal_id=deal.deal_id,
                        usd=self.usd,
                        crypto=self.crypto,
                        asset=deal.asset,
                        ltc_price=None,
                        show_buttons=False,
                    )
                    await interaction.message.edit(view=no_buttons_view)
                except Exception as e:
                    logger.warning(f"Button remove failed: {e}")

                await interaction.followup.send(
                    "✅ Both parties agreed! Preparing escrow address...",
                    ephemeral=True,
                )

                wallet = WalletManager()
                deal_index = int(hashlib.sha256(deal.deal_id.encode()).hexdigest()[:8], 16) % 1000000

                address = wallet.generate_address(
                    coin_symbol=deal.asset,
                    address_type=deal.address_type or "legacy",
                    index=deal_index,
                )

                logger.info(f"Address generated: {address} | Index: {deal_index}")

                await interaction.followup.send(
                    "🔷 Backing up deal data (5 locations)...",
                    ephemeral=True,
                )

                bm = get_backup_manager()
                backup_result = await bm.save_all(
                    deal_id=deal.deal_id,
                    index_num=deal_index,
                    address=address,
                    asset=deal.asset,
                    sender_id=deal.sender_id,
                    receiver_id=deal.receiver_id,
                )

                if not backup_result["verified"]:
                    logger.error(
                        f"Backup verification failed for {deal.deal_id} | "
                        f"Success: {backup_result['success_count']}/5"
                    )

                    await DatabaseManager.update_backup_status(
                        deal.deal_id, "FAILED", backup_result["locations"]
                    )
                    await DatabaseManager.update_status(deal.deal_id, "BACKUP_FAILED")

                    await interaction.followup.send(
                        "⚠️ **Backup system issue!**\n"
                        "Deal data could not be safely backed up.\n"
                        "Please wait for admin. **Do NOT send funds yet.**",
                        ephemeral=True,
                    )

                    try:
                        alert_embed = discord.Embed(
                            title="⚠️ Backup Failure Alert",
                            description=(
                                f"Deal `{deal.deal_id}` backup failed.\n\n"
                                f"**Success:** {backup_result['success_count']}/5\n"
                                f"**Locations:** `{backup_result['locations']}`\n\n"
                                f"Contact admin."
                            ),
                            color=discord.Color.red(),
                        )
                        await interaction.channel.send(embed=alert_embed)
                    except Exception:
                        pass
                    return

                await DatabaseManager.update_backup_status(
                    deal.deal_id,
                    "COMPLETE" if backup_result["success_count"] == backup_result.get("total_locations", 5) else "PARTIAL",
                    backup_result["locations"],
                )

                logger.info(f"Backup verified: {backup_result['success_count']}/5")

                try:
                    sender_user = await interaction.client.fetch_user(deal.sender_id) if deal.sender_id else None
                    receiver_user = await interaction.client.fetch_user(deal.receiver_id) if deal.receiver_id else None

                    sender_info = {
                        "user_id": str(deal.sender_id) if deal.sender_id else None,
                        "username": sender_user.name if sender_user else None,
                        "display_name": sender_user.display_name if sender_user else None,
                    }
                    receiver_info = {
                        "user_id": str(deal.receiver_id) if deal.receiver_id else None,
                        "username": receiver_user.name if receiver_user else None,
                        "display_name": receiver_user.display_name if receiver_user else None,
                    }

                    deal_amount = {
                        "usd": deal.usd_amount,
                        "crypto": deal.crypto_amount,
                        "asset": deal.asset,
                    }

                    await bm.save_extended_backup(
                        deal_id=deal.deal_id,
                        sender_info=sender_info,
                        receiver_info=receiver_info,
                        deal_amount=deal_amount,
                        crypto_address=address,
                        index_num=deal_index,
                    )
                except Exception as e:
                    logger.warning(f"Extended backup failed: {e}")

                await interaction.followup.send(
                    f"✅ Backup complete ({backup_result['success_count']}/5). Generating invoice...",
                    ephemeral=True,
                )

                await send_payment_invoice(
                    channel=interaction.channel,
                    deal=deal,
                    address=address,
                    crypto_amount=deal.crypto_amount,
                    usd_amount=deal.usd_amount,
                )
                logger.info(f"Deal {deal.deal_id} invoice generated.")

            else:
                await interaction.followup.send(
                    f"✅ Agreed! Waiting for the other user... ({clicked}/2)",
                    ephemeral=True,
                )
        except Exception as e:
            logger.error(f"btn_agree error: {e}", exc_info=True)
            try:
                await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass

    async def _on_cancel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        from Database import DatabaseManager

        deal = await self._get_deal(interaction)
        if not deal:
            await interaction.followup.send("❌ Deal not found.", ephemeral=True)
            return

        await DatabaseManager.update_status(deal.deal_id, "CANCELLED")

        try:
            channel = interaction.channel
            if channel:
                await asyncio.sleep(5)
                await channel.delete(reason="Deal cancelled by user")
        except Exception as e:
            logger.warning(f"Channel delete after cancel failed: {e}")

        await DatabaseManager.decrement_active_channels(
            [deal.sender_id, deal.receiver_id]
        )

        await interaction.followup.send("❌ Deal cancelled by one of the parties.", ephemeral=False)
        logger.info(f"Deal {deal.deal_id} cancelled by {interaction.user.id}")


import asyncio