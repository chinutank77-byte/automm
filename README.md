# automm

Discord escrow bot for Litecoin and Bitcoin deals. Buyer and seller pick roles,
agree on an amount, the bot hands out a fresh HD-wallet deposit address, watches
BlockCypher/SoChain for the payment, and holds it until the deal is released,
cancelled, or refunded.

It's written for one guild, so a lot of stuff (guild id, admin ids, role ids) is
set through `.env` rather than being dynamic.

## Stack

- Python 3.10+
- discord.py (slash commands + persistent views)
- bip-utils for the HD wallet (BIP44/49/84, legacy / p2sh / segwit / taproot)
- aiosqlite for storage
- BlockCypher + SoChain for payment detection
- Cloudflare R2 for off-site backups

## Setup

```bash
git clone https://github.com/chinutank77-byte/automm.git
cd automm
python -m venv venv
venv\Scripts\activate        # or `source venv/bin/activate` on linux
pip install -r requirements.txt
cp .env.example .env         # then edit it
python main.py
```

You need a Discord bot token and a BIP-39 seed phrase. The seed controls the
escrow wallet, so keep it out of git and ideally use a wallet you don't care
about while testing.

## Env vars

Everything is in `.env`. Copy `.env.example` and fill it in. The important ones:

| var | what it does |
|---|---|
| `DISCORD_TOKEN` | bot token |
| `GUILD_ID` | guild to sync slash commands to |
| `SEED_PHRASE` | BIP-39 seed for the escrow wallet |
| `ADMIN_USER_IDS` | comma-separated user ids allowed to run admin commands |
| `ADMIN_PIN_HASH` | sha256 of the admin PIN |

The rest (backup paths, R2 creds, rate limits, the second "link bot") are listed
in `.env.example` with comments.

## Commands

- `/setup_mm` — deploy the escrow panel (admin)
- `/ping`
- `/help`
- `/backup_status` — backup health (admin)
- `/backup_stats_deep` — backup + transcript stats (admin + PIN)
- `/force-release` — release funds to the seller (admin + PIN)
- `/force-cancel` — cancel and refund the buyer (admin + PIN)

## Todo / known issues

- No tests. Payment detection and transaction signing are the risky bits and
  they're only manually tested.
- Type hints are patchy.
- The backup/transcript path (R2 + discord-chat-exporter) has a lot of moving
  parts and fails quietly sometimes.
- No testnet mode — it's mainnet addresses, so be careful.

This handles real money. Use at your own risk.
