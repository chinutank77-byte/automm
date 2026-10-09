import aiosqlite
import logging
import json
import os
from dataclasses import dataclass
from typing import Optional, List, Tuple
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("DatabaseManager")


@dataclass
class Deal:
    deal_id: str
    channel_id: int
    guild_id: int
    sender_id: Optional[int]
    receiver_id: Optional[int]
    asset: str
    address_type: str
    usd_amount: Optional[float]
    crypto_amount: Optional[float]
    deposit_address: Optional[str]
    status: str
    txid: Optional[str]
    created_at: str
    updated_at: str
    backup_status: Optional[str] = "PENDING"
    user_check_confirmed: Optional[str] = None
    amount_agree_confirmed: Optional[str] = None
    channel_delete_at: Optional[str] = None


@dataclass
class BackupEntry:
    deal_id: str
    timestamp: str
    index_num: int
    address: str
    asset: str
    sender_id: Optional[int]
    receiver_id: Optional[int]
    backup_status: str
    backup_locations: Optional[str]


class DatabaseManager:
    _instance = None
    DB_PATH = "data/escrow.db"
    DATA_DIR = "data"
    _initialized = False

    CONNECTION_TIMEOUT = 30.0
    BUSY_TIMEOUT_MS = 30000

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
            logger.info("Database Manager instance created.")
        return cls._instance

    @classmethod
    async def _get_connection(cls) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(cls.DB_PATH, timeout=cls.CONNECTION_TIMEOUT)
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"PRAGMA busy_timeout={cls.BUSY_TIMEOUT_MS};")
        return conn

    @classmethod
    async def init_db(cls):
        try:
            os.makedirs(cls.DATA_DIR, exist_ok=True)
            logger.info(f"Data folder ready: {cls.DATA_DIR}/")
        except Exception as e:
            logger.error(f"Data folder creation failed: {e}")
            raise e

        conn = None
        try:
            conn = await aiosqlite.connect(cls.DB_PATH, timeout=cls.CONNECTION_TIMEOUT)
            conn.row_factory = aiosqlite.Row

            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute(f"PRAGMA busy_timeout={cls.BUSY_TIMEOUT_MS};")
            await conn.execute("PRAGMA wal_autocheckpoint=1000;")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS deals (
                    deal_id TEXT PRIMARY KEY,
                    channel_id INTEGER UNIQUE,
                    guild_id INTEGER,
                    sender_id INTEGER,
                    receiver_id INTEGER,
                    asset TEXT NOT NULL,
                    address_type TEXT DEFAULT 'legacy',
                    usd_amount REAL,
                    crypto_amount REAL,
                    deposit_address TEXT,
                    status TEXT NOT NULL,
                    txid TEXT,
                    backup_status TEXT DEFAULT 'PENDING',
                    user_check_confirmed TEXT DEFAULT '[]',
                    amount_agree_confirmed TEXT DEFAULT '[]',
                    channel_delete_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_id TEXT,
                    action TEXT,
                    actor_id INTEGER,
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (deal_id) REFERENCES deals(deal_id)
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS deal_backup (
                    deal_id TEXT PRIMARY KEY,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    index_num INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    sender_id INTEGER,
                    receiver_id INTEGER,
                    backup_status TEXT DEFAULT 'PENDING',
                    backup_locations TEXT,
                    FOREIGN KEY (deal_id) REFERENCES deals(deal_id)
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT DEFAULT 'PENDING',
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pin_attempts (
                    user_id INTEGER PRIMARY KEY,
                    attempt_count INTEGER DEFAULT 0,
                    first_attempt_at TIMESTAMP
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_deal_limits (
                    user_id INTEGER PRIMARY KEY,
                    deals_hour_start TIMESTAMP,
                    deals_hour_count INTEGER DEFAULT 0,
                    deals_day_start TIMESTAMP,
                    deals_day_count INTEGER DEFAULT 0,
                    active_channels INTEGER DEFAULT 0
                )
            """)

            await conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_channel ON deals(channel_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_sender ON deals(sender_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_receiver ON deals(receiver_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_delete_at ON deals(channel_delete_at);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_deal ON audit_logs(deal_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_backup_status ON deal_backup(backup_status);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON pending_tasks(status);")

            await conn.commit()
            cls._initialized = True
            logger.info("Database tables created (WAL mode ON).")

        except Exception as e:
            logger.error(f"init_db error: {e}")
            raise e
        finally:
            if conn:
                await conn.close()

    # ==========================================
    # DEAL MANAGEMENT
    # ==========================================
    @classmethod
    async def create_deal(cls, deal_id, channel_id, guild_id, asset, address_type="legacy") -> bool:
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute("""
                INSERT INTO deals (deal_id, channel_id, guild_id, asset, address_type, status, backup_status,
                                   user_check_confirmed, amount_agree_confirmed)
                VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '[]')
            """, (deal_id, channel_id, guild_id, asset, address_type, "WAITING_FOR_ROLES", "PENDING"))
            await conn.commit()
            await cls.add_audit_log(deal_id, "DEAL_CREATED", None, f"Asset: {asset}")
            logger.info(f"Deal {deal_id} created in channel {channel_id}.")
            return True
        except aiosqlite.IntegrityError:
            logger.error(f"Deal {deal_id} or channel {channel_id} already exists.")
            return False
        except Exception as e:
            logger.error(f"create_deal error: {e}")
            return False
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def get_deal_by_id(cls, deal_id) -> Optional[Deal]:
        conn = None
        try:
            conn = await cls._get_connection()
            async with conn.execute("SELECT * FROM deals WHERE deal_id = ?", (deal_id,)) as cursor:
                row = await cursor.fetchone()
                return Deal(**dict(row)) if row else None
        except Exception as e:
            logger.error(f"get_deal_by_id error: {e}")
            return None
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def get_deal_by_channel(cls, channel_id) -> Optional[Deal]:
        conn = None
        try:
            conn = await cls._get_connection()
            async with conn.execute("SELECT * FROM deals WHERE channel_id = ?", (channel_id,)) as cursor:
                row = await cursor.fetchone()
                return Deal(**dict(row)) if row else None
        except Exception as e:
            logger.error(f"get_deal_by_channel error: {e}")
            return None
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def update_roles(cls, deal_id, sender_id=None, receiver_id=None):
        conn = None
        try:
            conn = await cls._get_connection()
            if sender_id:
                await conn.execute(
                    "UPDATE deals SET sender_id = ?, updated_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                    (sender_id, deal_id)
                )
            if receiver_id:
                await conn.execute(
                    "UPDATE deals SET receiver_id = ?, updated_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                    (receiver_id, deal_id)
                )
            await conn.commit()
        except Exception as e:
            logger.error(f"update_roles error: {e}")
        finally:
            if conn:
                await conn.close()

        if sender_id:
            await cls.add_audit_log(deal_id, "SENDER_SET", sender_id)
        if receiver_id:
            await cls.add_audit_log(deal_id, "RECEIVER_SET", receiver_id)

    @classmethod
    async def reset_roles(cls, deal_id):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute("""
                UPDATE deals SET sender_id = NULL, receiver_id = NULL,
                                 user_check_confirmed = '[]', amount_agree_confirmed = '[]',
                                 updated_at = CURRENT_TIMESTAMP
                WHERE deal_id = ?
            """, (deal_id,))
            await conn.commit()
        except Exception as e:
            logger.error(f"reset_roles error: {e}")
        finally:
            if conn:
                await conn.close()
        await cls.add_audit_log(deal_id, "ROLES_RESET", None)

    # ==========================================
    # PERSISTENT TRACKERS
    # ==========================================
    @classmethod
    async def add_user_check_confirmation(cls, deal_id, user_id) -> int:
        conn = None
        try:
            conn = await cls._get_connection()
            async with conn.execute("SELECT user_check_confirmed FROM deals WHERE deal_id = ?", (deal_id,)) as cursor:
                row = await cursor.fetchone()
            if not row:
                return 0
            try:
                confirmed = json.loads(row["user_check_confirmed"] or "[]")
            except json.JSONDecodeError:
                confirmed = []
            if user_id not in confirmed:
                confirmed.append(user_id)
            await conn.execute(
                "UPDATE deals SET user_check_confirmed = ?, updated_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                (json.dumps(confirmed), deal_id)
            )
            await conn.commit()
            return len(confirmed)
        except Exception as e:
            logger.error(f"add_user_check_confirmation error: {e}")
            return 0
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def add_amount_agree_confirmation(cls, deal_id, user_id) -> int:
        conn = None
        try:
            conn = await cls._get_connection()
            async with conn.execute("SELECT amount_agree_confirmed FROM deals WHERE deal_id = ?", (deal_id,)) as cursor:
                row = await cursor.fetchone()
            if not row:
                return 0
            try:
                confirmed = json.loads(row["amount_agree_confirmed"] or "[]")
            except json.JSONDecodeError:
                confirmed = []
            if user_id not in confirmed:
                confirmed.append(user_id)
            await conn.execute(
                "UPDATE deals SET amount_agree_confirmed = ?, updated_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                (json.dumps(confirmed), deal_id)
            )
            await conn.commit()
            return len(confirmed)
        except Exception as e:
            logger.error(f"add_amount_agree_confirmation error: {e}")
            return 0
        finally:
            if conn:
                await conn.close()

    # ==========================================
    # STATUS UPDATES
    # ==========================================
    @classmethod
    async def update_amount(cls, deal_id, usd_amount, crypto_amount):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute("""
                UPDATE deals SET usd_amount = ?, crypto_amount = ?, status = 'WAITING_FOR_PAYMENT', updated_at = CURRENT_TIMESTAMP
                WHERE deal_id = ?
            """, (usd_amount, crypto_amount, deal_id))
            await conn.commit()
        except Exception as e:
            logger.error(f"update_amount error: {e}")
        finally:
            if conn:
                await conn.close()
        await cls.add_audit_log(deal_id, "AMOUNT_CONFIRMED", None, f"USD: {usd_amount}, Crypto: {crypto_amount}")

    @classmethod
    async def update_payment_info(cls, deal_id, deposit_address):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute("""
                UPDATE deals SET deposit_address = ?, status = 'WAITING_FOR_PAYMENT', updated_at = CURRENT_TIMESTAMP
                WHERE deal_id = ?
            """, (deposit_address, deal_id))
            await conn.commit()
        except Exception as e:
            logger.error(f"update_payment_info error: {e}")
        finally:
            if conn:
                await conn.close()
        await cls.add_audit_log(deal_id, "INVOICE_GENERATED", None, f"Address: {deposit_address}")

    @classmethod
    async def update_payment_detected(cls, deal_id, txid):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute("""
                UPDATE deals SET txid = ?, status = 'PAYMENT_DETECTED', updated_at = CURRENT_TIMESTAMP
                WHERE deal_id = ?
            """, (txid, deal_id))
            await conn.commit()
        except Exception as e:
            logger.error(f"update_payment_detected error: {e}")
        finally:
            if conn:
                await conn.close()
        await cls.add_audit_log(deal_id, "PAYMENT_DETECTED", None, f"TxID: {txid}")

    @classmethod
    async def update_status(cls, deal_id, status):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute(
                "UPDATE deals SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                (status, deal_id)
            )
            await conn.commit()
        except Exception as e:
            logger.error(f"update_status error: {e}")
        finally:
            if conn:
                await conn.close()
        await cls.add_audit_log(deal_id, "STATUS_CHANGED", None, f"New Status: {status}")

    @classmethod
    async def lock_deal_status(cls, deal_id, expected_status, new_status) -> bool:
        conn = None
        try:
            conn = await cls._get_connection()
            cursor = await conn.execute(
                "UPDATE deals SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE deal_id = ? AND status = ?",
                (new_status, deal_id, expected_status)
            )
            await conn.commit()
            success = cursor.rowcount > 0

            if success:
                await cls.add_audit_log(deal_id, "STATUS_LOCKED", None, f"{expected_status} -> {new_status}")
                logger.info(f"Status locked: {deal_id} | {expected_status} -> {new_status}")
            else:
                logger.warning(f"Status lock FAILED: {deal_id} | expected '{expected_status}'")

            return success
        except Exception as e:
            logger.error(f"lock_deal_status error: {e}")
            return False
        finally:
            if conn:
                await conn.close()

    # ==========================================
    # CHANNEL AUTO-DELETE
    # ==========================================
    @classmethod
    async def schedule_channel_delete(cls, deal_id, delay_seconds: int):
        conn = None
        try:
            delete_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).strftime("%Y-%m-%d %H:%M:%S")
            conn = await cls._get_connection()
            await conn.execute(
                "UPDATE deals SET channel_delete_at = ? WHERE deal_id = ?",
                (delete_at, deal_id)
            )
            await conn.commit()
            logger.info(f"Channel delete scheduled for {deal_id} at {delete_at}")
        except Exception as e:
            logger.error(f"schedule_channel_delete error: {e}")
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def get_deals_due_for_deletion(cls) -> List[Deal]:
        conn = None
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn = await cls._get_connection()
            async with conn.execute(
                "SELECT * FROM deals WHERE channel_delete_at IS NOT NULL AND channel_delete_at <= ?",
                (now,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [Deal(**dict(row)) for row in rows]
        except Exception as e:
            logger.error(f"get_deals_due_for_deletion error: {e}")
            return []
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def clear_channel_delete(cls, deal_id):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute(
                "UPDATE deals SET channel_delete_at = NULL WHERE deal_id = ?",
                (deal_id,)
            )
            await conn.commit()
        except Exception as e:
            logger.error(f"clear_channel_delete error: {e}")
        finally:
            if conn:
                await conn.close()

    # ==========================================
    # DEAL CREATE RATE LIMITS
    # ==========================================
    @classmethod
    async def check_deal_create_limit(cls, user_id, per_hour: int, per_day: int, max_active: int) -> Tuple[bool, str]:
        conn = None
        try:
            now = datetime.now(timezone.utc)
            conn = await cls._get_connection()

            async with conn.execute(
                "SELECT * FROM user_deal_limits WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                await conn.execute(
                    "INSERT INTO user_deal_limits (user_id, deals_hour_start, deals_hour_count, deals_day_start, deals_day_count, active_channels) VALUES (?, ?, 1, ?, 1, 1)",
                    (user_id, now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S"))
                )
                await conn.commit()
                return True, ""

            hour_start = datetime.strptime(row["deals_hour_start"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if row["deals_hour_start"] else now
            day_start = datetime.strptime(row["deals_day_start"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if row["deals_day_start"] else now
            hour_count = row["deals_hour_count"] or 0
            day_count = row["deals_day_count"] or 0
            active = row["active_channels"] or 0

            if (now - hour_start).total_seconds() >= 3600:
                hour_count = 0
                hour_start = now
            if (now - day_start).total_seconds() >= 86400:
                day_count = 0
                day_start = now

            if active >= max_active:
                return False, f"❌ You already have **{active}** active deals (max **{max_active}**). Finish or cancel some first."
            if hour_count >= per_hour:
                return False, f"❌ **Rate limit**: You can create max **{per_hour}** deals per hour. Try again later."
            if day_count >= per_day:
                return False, f"❌ **Rate limit**: You can create max **{per_day}** deals per day. Try again tomorrow."

            await conn.execute("""
                UPDATE user_deal_limits SET deals_hour_start = ?, deals_hour_count = ?, deals_day_start = ?, deals_day_count = ?, active_channels = ?
                WHERE user_id = ?
            """, (
                hour_start.strftime("%Y-%m-%d %H:%M:%S"),
                hour_count + 1,
                day_start.strftime("%Y-%m-%d %H:%M:%S"),
                day_count + 1,
                active + 1,
                user_id,
            ))
            await conn.commit()
            return True, ""
        except Exception as e:
            logger.error(f"check_deal_create_limit error: {e}")
            return True, ""
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def decrement_active_channels(cls, user_ids: List[int]):
        conn = None
        try:
            conn = await cls._get_connection()
            for uid in user_ids:
                if not uid:
                    continue
                await conn.execute(
                    "UPDATE user_deal_limits SET active_channels = MAX(0, active_channels - 1) WHERE user_id = ?",
                    (uid,)
                )
            await conn.commit()
            logger.info(f"Decremented active_channels for users: {user_ids}")
        except Exception as e:
            logger.error(f"decrement_active_channels error: {e}")
        finally:
            if conn:
                await conn.close()

    # ==========================================
    # PIN RATE LIMITS
    # ==========================================
    @classmethod
    async def check_pin_rate_limit(cls, user_id, max_per_minute: int) -> Tuple[bool, str]:
        conn = None
        try:
            now = datetime.now(timezone.utc)
            conn = await cls._get_connection()

            async with conn.execute(
                "SELECT * FROM pin_attempts WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                await conn.execute(
                    "INSERT INTO pin_attempts (user_id, attempt_count, first_attempt_at) VALUES (?, 1, ?)",
                    (user_id, now.strftime("%Y-%m-%d %H:%M:%S"))
                )
                await conn.commit()
                return True, ""

            first = datetime.strptime(row["first_attempt_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if row["first_attempt_at"] else now
            count = row["attempt_count"] or 0

            if (now - first).total_seconds() >= 60:
                await conn.execute(
                    "UPDATE pin_attempts SET attempt_count = 1, first_attempt_at = ? WHERE user_id = ?",
                    (now.strftime("%Y-%m-%d %H:%M:%S"), user_id)
                )
                await conn.commit()
                return True, ""

            if count >= max_per_minute:
                remaining = int(60 - (now - first).total_seconds())
                return False, f"🔒 **Too many PIN attempts** ({count}/{max_per_minute}). Try again in **{remaining}s**."

            await conn.execute(
                "UPDATE pin_attempts SET attempt_count = attempt_count + 1 WHERE user_id = ?",
                (user_id,)
            )
            await conn.commit()
            return True, ""
        except Exception as e:
            logger.error(f"check_pin_rate_limit error: {e}")
            return True, ""
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def reset_pin_attempts(cls, user_id):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute("DELETE FROM pin_attempts WHERE user_id = ?", (user_id,))
            await conn.commit()
        except Exception as e:
            logger.error(f"reset_pin_attempts error: {e}")
        finally:
            if conn:
                await conn.close()

    # ==========================================
    # PENDING TASKS QUEUE
    # ==========================================
    @classmethod
    async def enqueue_task(cls, task_type: str, payload: dict) -> int:
        conn = None
        try:
            conn = await cls._get_connection()
            cursor = await conn.execute(
                "INSERT INTO pending_tasks (task_type, payload, status) VALUES (?, ?, 'PENDING')",
                (task_type, json.dumps(payload))
            )
            await conn.commit()
            logger.info(f"Task enqueued: {task_type} | id={cursor.lastrowid}")
            return cursor.lastrowid
        except Exception as e:
            logger.error(f"enqueue_task error: {e}")
            return 0
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def get_pending_tasks(cls, limit: int = 10) -> List[dict]:
        conn = None
        try:
            conn = await cls._get_connection()
            async with conn.execute(
                "SELECT * FROM pending_tasks WHERE status IN ('PENDING', 'FAILED') AND attempts < 3 ORDER BY id ASC LIMIT ?",
                (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"get_pending_tasks error: {e}")
            return []
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def mark_task_complete(cls, task_id: int):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute(
                "UPDATE pending_tasks SET status = 'COMPLETED', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (task_id,)
            )
            await conn.commit()
            logger.info(f"Task completed: id={task_id}")
        except Exception as e:
            logger.error(f"mark_task_complete error: {e}")
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def mark_task_failed(cls, task_id: int, error: str):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute(
                "UPDATE pending_tasks SET status = 'FAILED', attempts = attempts + 1, last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (error[:500], task_id)
            )
            await conn.commit()
            logger.warning(f"Task failed: id={task_id} | {error[:100]}")
        except Exception as e:
            logger.error(f"mark_task_failed error: {e}")
        finally:
            if conn:
                await conn.close()

    # ==========================================
    # BACKUP
    # ==========================================
    @classmethod
    async def create_backup_entry(cls, deal_id, index_num, address, asset, sender_id=None, receiver_id=None) -> bool:
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute("""
                INSERT INTO deal_backup (deal_id, index_num, address, asset, sender_id, receiver_id, backup_status, backup_locations)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING', '{}')
            """, (deal_id, index_num, address, asset, sender_id, receiver_id))
            await conn.commit()
            logger.info(f"Backup entry created: {deal_id} | Index: {index_num}")
            return True
        except aiosqlite.IntegrityError:
            logger.warning(f"Backup entry exists: {deal_id}")
            return False
        except Exception as e:
            logger.error(f"create_backup_entry error: {e}")
            return False
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def update_backup_status(cls, deal_id, status, locations=None) -> bool:
        conn = None
        try:
            locations_json = json.dumps(locations or {})
            conn = await cls._get_connection()
            await conn.execute(
                "UPDATE deal_backup SET backup_status = ?, backup_locations = ? WHERE deal_id = ?",
                (status, locations_json, deal_id)
            )
            await conn.execute(
                "UPDATE deals SET backup_status = ? WHERE deal_id = ?",
                (status, deal_id)
            )
            await conn.commit()
            return True
        except Exception as e:
            logger.error(f"update_backup_status error: {e}")
            return False
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def get_backup_entry(cls, deal_id) -> Optional[BackupEntry]:
        conn = None
        try:
            conn = await cls._get_connection()
            async with conn.execute("SELECT * FROM deal_backup WHERE deal_id = ?", (deal_id,)) as cursor:
                row = await cursor.fetchone()
                return BackupEntry(**dict(row)) if row else None
        except Exception as e:
            logger.error(f"get_backup_entry error: {e}")
            return None
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def get_backup_stats(cls) -> dict:
        conn = None
        try:
            conn = await cls._get_connection()
            stats = {"total": 0, "complete": 0, "partial": 0, "pending": 0, "failed": 0}
            async with conn.execute("SELECT COUNT(*) as c FROM deal_backup") as cursor:
                row = await cursor.fetchone()
                stats["total"] = row["c"] if row else 0
            for status in ("COMPLETE", "PARTIAL", "PENDING", "FAILED"):
                async with conn.execute("SELECT COUNT(*) as c FROM deal_backup WHERE backup_status = ?", (status,)) as cursor:
                    row = await cursor.fetchone()
                    stats[status.lower()] = row["c"] if row else 0
            return stats
        except Exception as e:
            logger.error(f"get_backup_stats error: {e}")
            return {}
        finally:
            if conn:
                await conn.close()

    # ==========================================
    # HELPERS
    # ==========================================
    @classmethod
    async def get_pending_deals(cls) -> List[Deal]:
        return await cls.get_deal_by_status("WAITING_FOR_PAYMENT")

    @classmethod
    async def get_deal_by_status(cls, status) -> List[Deal]:
        conn = None
        try:
            conn = await cls._get_connection()
            async with conn.execute("SELECT * FROM deals WHERE status = ?", (status,)) as cursor:
                rows = await cursor.fetchall()
                return [Deal(**dict(row)) for row in rows]
        except Exception as e:
            logger.error(f"get_deal_by_status error: {e}")
            return []
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def add_audit_log(cls, deal_id, action, actor_id=None, details=None):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute(
                "INSERT INTO audit_logs (deal_id, action, actor_id, details) VALUES (?, ?, ?, ?)",
                (deal_id, action, actor_id, details)
            )
            await conn.commit()
        except Exception as e:
            logger.warning(f"Audit log save failed: {e}")
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def get_audit_logs(cls, deal_id) -> List[dict]:
        conn = None
        try:
            conn = await cls._get_connection()
            async with conn.execute(
                "SELECT * FROM audit_logs WHERE deal_id = ? ORDER BY timestamp ASC", (deal_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"get_audit_logs error: {e}")
            return []
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def get_all_active_deals(cls) -> List[Deal]:
        conn = None
        try:
            conn = await cls._get_connection()
            async with conn.execute("""
                SELECT * FROM deals
                WHERE status NOT IN ('COMPLETED', 'CANCELLED', 'REFUNDED')
                ORDER BY created_at DESC
            """) as cursor:
                rows = await cursor.fetchall()
                return [Deal(**dict(row)) for row in rows]
        except Exception as e:
            logger.error(f"get_all_active_deals error: {e}")
            return []
        finally:
            if conn:
                await conn.close()

    @classmethod
    async def delete_deal(cls, deal_id):
        conn = None
        try:
            conn = await cls._get_connection()
            await conn.execute("DELETE FROM audit_logs WHERE deal_id = ?", (deal_id,))
            await conn.execute("DELETE FROM deal_backup WHERE deal_id = ?", (deal_id,))
            await conn.execute("DELETE FROM deals WHERE deal_id = ?", (deal_id,))
            await conn.commit()
            logger.info(f"Deal {deal_id} deleted.")
        except Exception as e:
            logger.error(f"delete_deal error: {e}")
        finally:
            if conn:
                await conn.close()