<div align="center">

# 🛡️ AutoMM Escrow

**A feature-rich Discord escrow bot for Litecoin (LTC) & Bitcoin (BTC) deals.**

A self-custodial, HD-wallet powered escrow system with payment detection, multi-location backups, admin security, and a fully interactive button/slash-command UI.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![Litecoin](https://img.shields.io/badge/Coin-LTC%20%7C%20BTC-A6A9AA?logo=litecoin&logoColor=white)](https://litecoin.org/)
[![License](https://img.shields.io/badge/License-MIT-8A2BE2)](./LICENSE)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen)]()
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)]()

</div>

---

## ✨ Features

| Area | Details |
|---|---|
| 🔐 **HD Wallet** | BIP-44 / BIP-49 / BIP-84 deterministic address generation for LTC & BTC (Legacy, P2SH, Native SegWit, Taproot). One seed → infinite unique deposit addresses per deal. |
| 🤝 **Escrow Flow** | Role selection → amount agreement → deposit address → payment detection → release / cancel / refund. |
| 📡 **Payment Detection** | Background task polls BlockCypher & SoChain for incoming transactions and auto-advances the deal. |
| 💾 **Multi-location Backups** | JSON + TXT + Cloudflare R2 (S3-compatible) + chat transcript (via `discord-chat-exporter`). Backup health tracked in DB. |
| 🛡️ **Admin Security** | Admin user whitelist + hashed PIN + per-minute PIN rate-limiting + audit logging. |
| 🧾 **Invoices** | QR-code invoices with a unique deposit address per deal. |
| 🎛️ **Interactive UI** | Persistent buttons & slash commands (no legacy prefix commands needed). |
| 🚦 **Rate Limiting** | Per-user deal limits (hourly / daily / concurrent active channels). |
| 🧹 **Auto-cleanup** | Scheduled deal-channel deletion, orphan temp cleanup, rotating logs. |

---

## 🧰 Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| Discord | `discord.py` (async, slash commands, persistent views) |
| Crypto | `bip-utils` (BIP-44/49/84), `ecdsa`, `base58` |
| Blockchain APIs | BlockCypher, SoChain |
| Storage | `aiosqlite` (SQLite, WAL mode), Cloudflare R2 |
| Backups | JSON / TXT files, R2, `discord-chat-exporter` transcripts |
| Async HTTP | `aiohttp`, `aiofiles` |
| Config | `python-dotenv` (`.env`) |

---

## 📁 Project Structure

```
automm/
├── main.py                 # Bot entrypoint — intents, slash commands, views, logging
├── Database.py             # SQLite layer (deals, backups, rate-limits, audit, task queue)
├── wallet.py               # HD wallet — LTC/BTC address generation + validation
├── send.py                 # Transaction signing & broadcasting
├── common.py               # Shared wallet helpers
├── RoleSelection.py        # Role-selection / amount-agreement panels
├── DealConfirmation.py     # Deal confirmation view
├── Invoice.py              # QR invoice generation
├── Backgroundtask.py       # Payment checker & background jobs
├── ui/                     # PaymentDetected UI + views
├── buttons/                # release / cancel admin buttons
├── security/               # admin_utils (whitelist, PIN, rate-limit)
├── backup_manager/         # backup + transcript managers
├── .env                    # Environment configuration (see below)
├── requirements.txt        # Python dependencies
└── README.md
```

---

## 🚀 Quick Start

### 1. Prerequisites

- Python **3.10** or higher
- A Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))
- A BIP-39 **seed phrase** (kept secret — see Security below)
- *(Optional)* BlockCypher / SoChain API keys, Cloudflare R2 credentials

### 2. Install

```bash
# Clone
git clone https://github.com/chinutank77-byte/automm.git
cd automm

# (Recommended) create a virtual environment
python -m venv venv
# Windows
venv\Scripts\activate
# Linux/macOS
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure `.env`

Rename `.env` (or create one) and fill in the values:

```bash
cp .env.example .env   # if an example is provided
```

### 4. Run

```bash
python main.py
```

On first start the bot creates the SQLite DB (`data/escrow.db`) and syncs slash commands to your guild.

---

## ⚙️ Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Your Discord bot token |
| `GUILD_ID` | Target guild (server) ID for command sync |
| `SEED_PHRASE` | BIP-39 HD wallet seed phrase (**keep secret!**) |
| `BLOCKCYPHER_TOKEN` | BlockCypher API token (payment detection) |
| `SOCHAIN_API_KEY` | SoChain API key (payment detection) |
| `ADMIN_USER_IDS` | Comma-separated Discord user IDs allowed admin access |
| `ADMIN_PIN_HASH` | Hashed admin PIN |
| `PIN_MAX_ATTEMPTS_PER_MINUTE` | PIN brute-force rate limit |
| `STAFF_ROLE_ID` | Staff role ID |
| `STAFF_NOTIFY_CHANNEL_ID` | Channel for staff notifications |
| `BACKUP_JSON_PATH` / `BACKUP_TXT_PATH` | Local backup file paths |
| `MIN_BACKUP_SUCCESS` | Minimum backups required before release |
| `DISCORD_BACKUP_WEBHOOK` | Webhook URL for backup notifications |
| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | Cloudflare R2 credentials |
| `R2_BUCKET_NAME` / `R2_ENDPOINT` | R2 bucket config |
| `CHANNEL_DELETE_DELAY_SECONDS` | Delay before auto-deleting a deal channel |
| `DEAL_RATE_LIMIT_PER_HOUR` / `DEAL_RATE_LIMIT_PER_DAY` | Per-user deal creation limits |
| `MAX_CONCURRENT_DEALS` | Max active deals per user |
| `LINKBOT_TOKEN` | Secondary bot token (linking) |
| `MEMBER_ROLE_ID` | Member role ID |
| `STATUS_TRIGGER` | Payment-check status trigger |
| `SCAN_INTERVAL_MINUTES` / `SCAN_CHUNK_SIZE` / `SCAN_CHUNK_DELAY_SECONDS` | Payment scan tuning |
| `AUTO_REMOVE_ROLE` | Whether to auto-remove a role after deal |

---

## 🎮 Commands

| Command | Permission | Description |
|---|---|---|
| `/setup_mm` | Admin | Deploy the Auto-MM escrow panel |
| `/ping` | Everyone | Check bot latency |
| `/help` | Everyone | Show command help |
| `/backup_status` | Admin | Backup system statistics |
| `/backup_stats_deep` | Admin + PIN | Deep backup + transcript statistics |
| `/force-release` | Admin + PIN | Force-release funds to seller (`deal_id` optional) |
| `/force-cancel` | Admin + PIN | Force-cancel deal & refund buyer (`deal_id` optional) |

---

## 🔄 Deal Lifecycle

```
WAITING_FOR_ROLES → amount agreed → WAITING_FOR_PAYMENT → PAYMENT_DETECTED
                                                                  │
                                          ┌───────────────────────┼───────────────────────┐
                                          ▼                       ▼                       ▼
                                      COMPLETED                CANCELLED               REFUNDED
                                      (released)               (canceled)              (buyer refund)
```

1. Admin runs `/setup_mm` to deploy the panel.
2. Users pick **sender / receiver** roles and agree on the amount.
3. A **unique HD deposit address** is derived per deal and shown as a QR invoice.
4. The background checker detects the payment via blockchain APIs.
5. Funds are **released**, **canceled**, or **refunded** via buttons or admin commands.

---

## 🔒 Security

- 🔑 The **seed phrase** is the single point of control over all deal funds — **never commit it** and never share it.
- 🧑‍💼 Admin actions are gated by a **user whitelist + hashed PIN + rate-limiting**.
- 📝 Every deal action is written to an **audit log** table.
- ⚠️ **Private keys are derived on-demand** and are *not* persisted to the database.

> **Disclaimer:** This project is provided for educational / authorized use only. The authors are not responsible for loss of funds, misuse, or any consequence of running this software. Always test on testnet first.

---

## 📄 License

Released under the **MIT License**.

---

<div align="center">

**Made with 💜 for the AutoMM community.**

</div>
