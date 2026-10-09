# ==========================================
# ui/PaymentDetected.py
# Components V2 LayoutView — Payment Detected message
# Requires: discord.py 2.4+
# ==========================================
import discord
from discord import ui
import logging

# Version check
if discord.version_info < (2, 4):
    raise ImportError(
        "discord.py 2.4+ required for Components V2. "
        "Run: pip install -U discord.py"
    )

logger = logging.getLogger("PaymentDetected")

# ==========================================
# CONFIGURATION
# ==========================================
ACCENT_COLOR = discord.Color.from_rgb(138, 92, 246)   # #8a5cf6
EXPLORER_BASE = "https://live.blockcypher.com/ltc/tx"

# Loading GIF (side me dikhega — Section accessory)
LOADING_GIF = (
    "https://cdn.discordapp.com/attachments/1441783975000412365/"
    "1451185211776831583/43294-youtube-loading-circle.gif"
    "?ex=6ac8314c&is=6ac6dfcc&hm="
    "827901f14a50dd06243571c6b6243d950c82b9b6041f4ce14c77b0955ada5ef3"
)


# ==========================================
# PAYMENT DETECTED LAYOUT (Components V2)
# ==========================================
class PaymentDetectedView(ui.LayoutView):
    """
    Components V2 LayoutView — 'Payment Detected' message.

    Format:
        ## Payment Detected
        * **Status** → Awaiting block confirmation
        * **Amount** → `X.XXXXXXXX LTC`($Y.YY)
        * **Confirmations** → `0/1`
        * **Transaction ID** → [abc123..](explorer-link)
        * **Sender** → @user(`id`)
        * **Receiver** → @user(`id`)

        MM • Processing block receipt

    Side me loading GIF (Thumbnail accessory).
    """

    def __init__(
        self,
        deal_id: str = None,
        sender_id: int = None,
        receiver_id: int = None,
        amount: float = 0.0,
        usd_amount: float = 0.0,
        asset: str = "LTC",
        confirmations: int = 0,
        required_confirmations: int = 1,
        txid: str = None,
    ):
        super().__init__(timeout=None)

        # ---- Safe defaults (persistence registration ke liye) ----
        txid = txid or "N/A"
        txid_short = f"{txid[:12]}.." if len(txid) > 12 else txid
        explorer_url = f"{EXPLORER_BASE}/{txid}" if txid != "N/A" else EXPLORER_BASE

        # ---- Mentions ----
        sender_line = f"<@{sender_id}>(`{sender_id}`)" if sender_id else "`N/A`"
        receiver_line = f"<@{receiver_id}>(`{receiver_id}`)" if receiver_id else "`N/A`"

        # ---- Body markdown ----
        body = (
            f"## Payment Detected\n"
            f"* **Status** → Awaiting block confirmation\n"
            f"* **Amount** → `{amount:.8f} {asset}`(${usd_amount})\n"
            f"* **Confirmations** → `{confirmations}/{required_confirmations}`\n"
            f"* **Transaction ID** → [{txid_short}]({explorer_url})\n"
            f"* **Sender** → {sender_line}\n"
            f"* **Receiver** → {receiver_line}"
        )

        # ---- Container (accent bar on left) ----
        container = ui.Container(accent_color=ACCENT_COLOR)

        # ---- Section with side thumbnail (GIF) ----
        try:
            section = ui.Section(
                ui.TextDisplay(body),
                accessory=ui.Thumbnail(LOADING_GIF),
            )
            container.add_item(section)
        except Exception as e:
            logger.warning(f"⚠️ Thumbnail accessory fail ({e}) — text-only fallback")
            container.add_item(ui.TextDisplay(body))

        # ---- Separator + Footer ----
        container.add_item(ui.Separator())
        container.add_item(ui.TextDisplay("-# MM • Processing block receipt"))

        self.add_item(container)


# ==========================================
# SEND / REPLACE PAYMENT DETECTED MESSAGE
# ==========================================
async def send_payment_detected(
    bot: discord.Client,
    deal,
    txid: str,
    amount: float,
    confirmations: int,
    required_confirmations: int = 1,
):
    """
    Purane 'Waiting for Payment' (embed) message ko dhundhkar delete karta hai
    aur naya 'Payment Detected' LayoutView bhejta hai.
    """
    try:
        channel = bot.get_channel(deal.channel_id)
        if not channel:
            logger.warning(f"⚠️ Channel {deal.channel_id} nahi mila")
            return False

        # ---- Purana "Waiting for Payment" message dhundho ----
        target_msg = None
        async for msg in channel.history(limit=100):
            if msg.author != bot.user:
                continue
            if not msg.embeds:
                continue
            title = (msg.embeds[0].title or "").lower()
            if "waiting for payment" in title or "payment detected" in title:
                target_msg = msg
                break

        # ---- Delete purana (agar mila) ----
        if target_msg:
            try:
                await target_msg.delete()
                logger.debug(f"🗑️ Purana message delete: {target_msg.id}")
            except (discord.NotFound, discord.Forbidden) as e:
                logger.warning(f"⚠️ Purana message delete nahi hua: {e}")

        # ---- Naya LayoutView bhejo ----
        layout = PaymentDetectedView(
            deal_id=deal.deal_id,
            sender_id=deal.sender_id,
            receiver_id=deal.receiver_id,
            amount=amount,
            usd_amount=deal.usd_amount,
            asset=deal.asset,
            confirmations=confirmations,
            required_confirmations=required_confirmations,
            txid=txid,
        )

        await channel.send(view=layout)
        logger.info(f"✅ Payment Detected message bheja — Deal {deal.deal_id}")
        return True

    except Exception as e:
        logger.error(f"❌ send_payment_detected error: {e}", exc_info=True)
        return False