import discord
import qrcode
import io
import logging

logger = logging.getLogger("InvoiceManager")

BRAND_COLOR = discord.Color(0x0303d4)
EXPLORER_BASE = "https://live.blockcypher.com/ltc/tx"

QR_BOX_SIZE = 5
QR_BORDER = 2


def generate_qr_code(data: str) -> discord.File:
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=QR_BOX_SIZE,
            border=QR_BORDER,
        )
        qr.add_data(data)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)

        return discord.File(buffer, filename="qr_code.png")
    except Exception as e:
        logger.error(f"QR Code generation failed: {e}")
        return None


class InvoiceView(discord.ui.View):
    def __init__(self, address: str = None, amount: float = None, asset: str = None):
        super().__init__(timeout=None)
        self.address = address
        self.amount = amount
        self.asset = asset

    async def _fetch_deal_data(self, interaction: discord.Interaction):
        from Database import DatabaseManager

        if self.address and self.amount and self.asset:
            return self.address, self.amount, self.asset

        deal = await DatabaseManager.get_deal_by_channel(interaction.channel_id)
        if deal and deal.deposit_address:
            return deal.deposit_address, deal.crypto_amount, deal.asset
        return None, None, None

    @discord.ui.button(
        label="Copy Details",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_copy_details"
    )
    async def copy_details(self, interaction: discord.Interaction, button: discord.ui.Button):
        address, amount, asset = await self._fetch_deal_data(interaction)

        if not address:
            await interaction.response.send_message(
                "❌ Deal details not found. Please contact staff.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📋 Payment Details",
            color=BRAND_COLOR
        )
        embed.add_field(name="Address", value=f"`{address}`", inline=False)
        embed.add_field(name="Amount", value=f"`{amount} {asset}`", inline=False)
        embed.set_footer(text="Crazy MM & Exchange")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Generate QR Code",
        style=discord.ButtonStyle.primary,
        custom_id="btn_gen_qr"
    )
    async def gen_qr(self, interaction: discord.Interaction, button: discord.ui.Button):
        address, amount, asset = await self._fetch_deal_data(interaction)

        if not address:
            await interaction.response.send_message(
                "❌ Address not found. Please contact staff.",
                ephemeral=True
            )
            return

        qr_file = generate_qr_code(address)
        if qr_file:
            embed = discord.Embed(
                title="📱 Scan to Pay",
                description=f"Amount: `{amount} {asset}`",
                color=discord.Color.green()
            )
            embed.set_image(url="attachment://qr_code.png")
            embed.set_footer(text="Crazy MM & Exchange")

            await interaction.response.send_message(
                embed=embed,
                file=qr_file,
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ QR Code generation failed.",
                ephemeral=True
            )


async def send_payment_invoice(
    channel: discord.TextChannel,
    deal,
    address: str,
    crypto_amount: float,
    usd_amount: float
):
    from Database import DatabaseManager

    try:
        await DatabaseManager.update_payment_info(deal.deal_id, address)

        asset = deal.asset
        deal_id = deal.deal_id

        invoice_embed = discord.Embed(
            title="Payment Invoice",
            description=(
                f"<@{deal.sender_id}>, send your funds to the escrow address below.\n"
                f"Copy the amount exactly so we can match your payment."
            ),
            color=BRAND_COLOR
        )
        invoice_embed.add_field(
            name="Security",
            value=(
                "Only the sender should send this payment. "
                "Do not forward this address to anyone else."
            ),
            inline=False
        )
        invoice_embed.add_field(
            name="Escrow Address",
            value=f"`{address}`",
            inline=False
        )
        invoice_embed.add_field(
            name="Amount to Send",
            value=f"`{crypto_amount} {asset}` | `${usd_amount} USD`",
            inline=False
        )
        invoice_embed.add_field(
            name="Reference",
            value=f"Deal ID `{deal_id}`",
            inline=False
        )

        qr_file = generate_qr_code(address)
        view = InvoiceView(address=address, amount=crypto_amount, asset=asset)

        if qr_file:
            invoice_embed.set_thumbnail(url="attachment://qr_code.png")
            await channel.send(embed=invoice_embed, file=qr_file, view=view)
        else:
            invoice_embed.set_thumbnail(
                url="https://cryptologos.cc/logos/litecoin-ltc-logo.png"
            )
            await channel.send(embed=invoice_embed, view=view)

        invoice_embed.set_footer(text="Crazy MM & Exch")

        waiting_embed = discord.Embed(
            title="Waiting for Payment",
            description="The bot is checking the blockchain for your payment.",
            color=discord.Color.orange()
        )
        waiting_embed.add_field(
            name="Required Amount",
            value=f"`{crypto_amount} {asset}` | `${usd_amount} USD`",
            inline=False
        )
        waiting_embed.add_field(
            name="Wallet Address",
            value=f"`{address}`",
            inline=False
        )
        waiting_embed.add_field(
            name="Status",
            value="Checking for payment. Please wait.",
            inline=False
        )
        waiting_embed.set_thumbnail(
            url="https://cryptologos.cc/logos/litecoin-ltc-logo.png"
        )
        waiting_embed.set_footer(text="Crazy MM & Exch")

        await channel.send(embed=waiting_embed)
        logger.info(f"Deal {deal_id} invoice and waiting message sent.")

    except Exception as e:
        logger.error(f"Invoice send error: {e}", exc_info=True)