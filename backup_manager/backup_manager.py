import os
import json
import logging
import asyncio
import aiofiles
import aiohttp
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("BackupManager")

BASE_DIR = Path(__file__).resolve().parent.parent

BACKUP_DIR = BASE_DIR / "backup"
DETAIL_BACKUP_DIR = BASE_DIR / "detail_backup"
DETAIL_CONFIG_PATH = DETAIL_BACKUP_DIR / "config.json"
DETAIL_TEMP_DIR = DETAIL_BACKUP_DIR / "temp"
DATA_DIR = BASE_DIR / "data"


class BackupConfig:
    JSON_PATH = os.getenv("BACKUP_JSON_PATH", str(BACKUP_DIR / "backup.json"))
    TXT_PATH = os.getenv("BACKUP_TXT_PATH", str(BACKUP_DIR / "backup.txt"))

    DISCORD_WEBHOOK = os.getenv("DISCORD_BACKUP_WEBHOOK", "").strip()

    R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "").strip()
    R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
    R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
    R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "").strip()
    R2_ENDPOINT = os.getenv("R2_ENDPOINT", "").strip()

    MIN_BACKUP_SUCCESS = int(os.getenv("MIN_BACKUP_SUCCESS", "3"))
    TOTAL_LOCATIONS = 7
    REQUEST_TIMEOUT = 15
    MAX_RETRIES = 3

    SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
    SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
    SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "escrow_backups").strip()

    UPSTASH_REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
    UPSTASH_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()


def _load_cloud_config() -> dict:
    try:
        if not DETAIL_CONFIG_PATH.exists():
            logger.warning(f"Cloud config not found: {DETAIL_CONFIG_PATH}")
            return {"cloud_storages": []}

        with open(DETAIL_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "cloud_storages" not in data:
            return {"cloud_storages": []}
        return data
    except Exception as e:
        logger.error(f"Failed to load cloud config: {e}")
        return {"cloud_storages": []}


class BackupManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(BackupManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._ensure_folders()

        self._json_lock = asyncio.Lock()
        self._txt_lock = asyncio.Lock()
        self._detail_lock = asyncio.Lock()

        self._session: Optional[aiohttp.ClientSession] = None
        self._s3_clients: dict = {}

        self._check_config()

        logger.info("BackupManager initialized.")

    def _ensure_folders(self):
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            DETAIL_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            DETAIL_TEMP_DIR.mkdir(parents=True, exist_ok=True)
            DATA_DIR.mkdir(parents=True, exist_ok=True)

            Path(BackupConfig.JSON_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(BackupConfig.TXT_PATH).parent.mkdir(parents=True, exist_ok=True)

            logger.info(
                f"Folders ready: {BACKUP_DIR.name}/, {DETAIL_BACKUP_DIR.name}/, {DATA_DIR.name}/"
            )
        except Exception as e:
            logger.error(f"Folder creation failed: {e}")

    def _check_config(self):
        cfg = BackupConfig
        if not cfg.DISCORD_WEBHOOK:
            logger.warning("DISCORD_BACKUP_WEBHOOK not set — Discord backup disabled.")
        if not (cfg.R2_ACCOUNT_ID and cfg.R2_ACCESS_KEY_ID and cfg.R2_SECRET_ACCESS_KEY):
            logger.warning("R2/S3 credentials not set — simple cloud backup disabled.")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=BackupConfig.REQUEST_TIMEOUT)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

        for name, client in list(self._s3_clients.items()):
            try:
                client.close()
            except Exception:
                pass
        self._s3_clients.clear()

    async def save_all(
        self,
        deal_id: str,
        index_num: int,
        address: str,
        asset: str,
        sender_id: int = None,
        receiver_id: int = None,
        timestamp: Optional[str] = None,
    ) -> dict:
        ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        logger.info(
            f"Backup started for {deal_id} | Index: {index_num} | "
            f"Sender: {sender_id} | Seller: {receiver_id}"
        )

        tasks = {
            "sqlite": self._save_to_sqlite(deal_id, index_num, address, asset, sender_id, receiver_id),
            "json": self._save_to_json(deal_id, ts, index_num, address, asset, sender_id, receiver_id),
            "txt": self._save_to_txt(deal_id, ts, index_num, address, asset, sender_id, receiver_id),
            "discord": self._save_to_discord(deal_id, ts, index_num, address, asset, sender_id, receiver_id),
            "cloud": self._save_to_cloud(deal_id, ts, index_num, address, asset, sender_id, receiver_id),
            "supabase": self._save_to_supabase(deal_id, ts, index_num, address, asset, sender_id, receiver_id),
            "redis": self._save_to_redis(deal_id, ts, index_num, address, asset, sender_id, receiver_id),
        }

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        locations = {}
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"Backup [{name}] failed: {result}")
                locations[name] = False
            else:
                locations[name] = bool(result)

        success_count = sum(1 for v in locations.values() if v)
        failed_count = len(locations) - success_count
        verified = success_count >= BackupConfig.MIN_BACKUP_SUCCESS

        logger.info(
            f"Backup done: {success_count}/{BackupConfig.TOTAL_LOCATIONS} | "
            f"Verified: {verified} | Locations: {locations}"
        )

        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "locations": locations,
            "verified": verified,
            "total_locations": self._configured_location_count(),
            "timestamp": ts,
        }

    async def _save_to_sqlite(
        self,
        deal_id: str,
        index_num: int,
        address: str,
        asset: str,
        sender_id: int = None,
        receiver_id: int = None,
    ) -> bool:
        try:
            from Database import DatabaseManager

            existing = await DatabaseManager.get_backup_entry(deal_id)
            if existing:
                return await DatabaseManager.update_backup_status(deal_id, "PARTIAL")

            return await DatabaseManager.create_backup_entry(
                deal_id, index_num, address, asset, sender_id, receiver_id
            )
        except Exception as e:
            logger.error(f"SQLite backup failed: {e}")
            return False

    async def _save_to_json(
        self,
        deal_id: str,
        ts: str,
        index_num: int,
        address: str,
        asset: str,
        sender_id: int = None,
        receiver_id: int = None,
    ) -> bool:
        async with self._json_lock:
            try:
                path = Path(BackupConfig.JSON_PATH)
                path.parent.mkdir(parents=True, exist_ok=True)

                data = {"deals": [], "version": "1.0"}
                if path.exists():
                    try:
                        async with aiofiles.open(path, "r", encoding="utf-8") as f:
                            content = await f.read()
                            if content.strip():
                                data = json.loads(content)
                    except (json.JSONDecodeError, Exception) as e:
                        logger.warning(f"JSON corrupt, creating new file: {e}")
                        data = {"deals": [], "version": "1.0"}

                data["deals"].append({
                    "deal_id": deal_id,
                    "timestamp": ts,
                    "index": index_num,
                    "address": address,
                    "asset": asset,
                    "sender_id": sender_id,
                    "receiver_id": receiver_id,
                })
                data["last_updated"] = datetime.now(timezone.utc).isoformat()

                tmp_path = path.with_suffix(".json.tmp")
                async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(data, indent=2, ensure_ascii=False))

                if path.exists():
                    path.unlink()
                tmp_path.rename(path)

                logger.debug(f"JSON backup done for {deal_id}")
                return True

            except Exception as e:
                logger.error(f"JSON backup failed: {e}")
                return False

    async def _save_to_txt(
        self,
        deal_id: str,
        ts: str,
        index_num: int,
        address: str,
        asset: str,
        sender_id: int = None,
        receiver_id: int = None,
    ) -> bool:
        async with self._txt_lock:
            try:
                path = Path(BackupConfig.TXT_PATH)
                path.parent.mkdir(parents=True, exist_ok=True)

                sid = sender_id if sender_id is not None else "unknown"
                rid = receiver_id if receiver_id is not None else "unknown"
                line = f"{deal_id} : {ts} : {index_num} : {address} : {asset} : {sid} : {rid}\n"

                async with aiofiles.open(path, "a", encoding="utf-8") as f:
                    await f.write(line)

                logger.debug(f"TXT backup done for {deal_id}")
                return True

            except Exception as e:
                logger.error(f"TXT backup failed: {e}")
                return False

    async def _save_to_discord(
        self,
        deal_id: str,
        ts: str,
        index_num: int,
        address: str,
        asset: str,
        sender_id: int = None,
        receiver_id: int = None,
    ) -> bool:
        if not BackupConfig.DISCORD_WEBHOOK:
            return False

        try:
            session = await self._get_session()

            sender_display = f"<@{sender_id}> (`{sender_id}`)" if sender_id else "`N/A`"
            receiver_display = f"<@{receiver_id}> (`{receiver_id}`)" if receiver_id else "`N/A`"

            embed = {
                "title": "📋 Deal Backup",
                "color": 0x0303d4,
                "fields": [
                    {"name": "Deal ID", "value": f"`{deal_id}`", "inline": False},
                    {"name": "Timestamp", "value": f"`{ts}`", "inline": True},
                    {"name": "Index", "value": f"`{index_num}`", "inline": True},
                    {"name": "Asset", "value": f"`{asset}`", "inline": True},
                    {"name": "Sender", "value": sender_display, "inline": False},
                    {"name": "Seller", "value": receiver_display, "inline": False},
                    {"name": "Address", "value": f"`{address}`", "inline": False},
                ],
                "footer": {"text": "Crazy MM • Auto-Backup System"},
            }

            deal_json = json.dumps({
                "deal_id": deal_id,
                "timestamp": ts,
                "index": index_num,
                "address": address,
                "asset": asset,
                "sender_id": sender_id,
                "receiver_id": receiver_id,
            }, indent=2)

            form = aiohttp.FormData()
            form.add_field(
                "payload_json",
                json.dumps({"embeds": [embed]}),
                content_type="application/json",
            )
            form.add_field(
                "file",
                deal_json.encode("utf-8"),
                filename=f"backup_{deal_id}.json",
                content_type="application/json",
            )

            async with session.post(BackupConfig.DISCORD_WEBHOOK, data=form) as resp:
                if resp.status in (200, 204):
                    logger.debug(f"Discord backup done for {deal_id}")
                    return True
                body = await resp.text()
                logger.error(f"Discord backup HTTP {resp.status}: {body[:200]}")
                return False

        except asyncio.TimeoutError:
            logger.error(f"Discord backup timeout for {deal_id}")
            return False
        except Exception as e:
            logger.error(f"Discord backup failed: {e}")
            return False

    async def _save_to_cloud(
        self,
        deal_id: str,
        ts: str,
        index_num: int,
        address: str,
        asset: str,
        sender_id: int = None,
        receiver_id: int = None,
    ) -> bool:
        cfg = BackupConfig

        if not (cfg.R2_ACCOUNT_ID and cfg.R2_ACCESS_KEY_ID and cfg.R2_SECRET_ACCESS_KEY):
            logger.debug("Cloud backup skipped — credentials not configured.")
            return False

        try:
            client = await self._init_s3_client()
            if client is None:
                return False

            object_key = f"deals/{deal_id}.json"

            content = json.dumps({
                "deal_id": deal_id,
                "timestamp": ts,
                "index": index_num,
                "address": address,
                "asset": asset,
                "sender_id": sender_id,
                "receiver_id": receiver_id,
            }, indent=2).encode("utf-8")

            await asyncio.to_thread(
                client.put_object,
                Bucket=cfg.R2_BUCKET_NAME,
                Key=object_key,
                Body=content,
                ContentType="application/json",
            )

            logger.debug(f"Cloud backup done for {deal_id} -> {object_key}")
            return True

        except Exception as e:
            logger.error(f"Cloud backup failed: {e}")
            return False

    async def _init_s3_client(self):
        cfg = BackupConfig

        endpoint = cfg.R2_ENDPOINT
        if not endpoint and cfg.R2_ACCOUNT_ID:
            endpoint = f"https://{cfg.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

        if not endpoint:
            logger.error("R2_ENDPOINT missing.")
            return None

        cache_key = f"{endpoint}|{cfg.R2_BUCKET_NAME}|{cfg.R2_ACCESS_KEY_ID}"
        if cache_key in self._s3_clients:
            return self._s3_clients[cache_key]

        try:
            import boto3
            from botocore.config import Config as BotoConfig

            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=cfg.R2_ACCESS_KEY_ID,
                aws_secret_access_key=cfg.R2_SECRET_ACCESS_KEY,
                region_name="auto",
                config=BotoConfig(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=10,
                    read_timeout=10,
                ),
            )
            self._s3_clients[cache_key] = client
            logger.info("S3/R2 client initialized.")
            return client

        except ImportError:
            logger.error("boto3 not installed. Run: pip install boto3")
            return None
        except Exception as e:
            logger.error(f"S3 client init failed: {e}")
            return None

    def _configured_location_count(self) -> int:
        cfg = BackupConfig
        n = 3  # sqlite, json, txt (always available)
        if cfg.DISCORD_WEBHOOK:
            n += 1
        if cfg.R2_ACCOUNT_ID and cfg.R2_ACCESS_KEY_ID and cfg.R2_SECRET_ACCESS_KEY:
            n += 1
        if cfg.SUPABASE_URL and cfg.SUPABASE_KEY:
            n += 1
        if cfg.UPSTASH_REDIS_URL and cfg.UPSTASH_REDIS_TOKEN:
            n += 1
        return n

    async def _save_to_supabase(
        self,
        deal_id: str,
        ts: str,
        index_num: int,
        address: str,
        asset: str,
        sender_id: int = None,
        receiver_id: int = None,
    ) -> bool:
        cfg = BackupConfig
        if not (cfg.SUPABASE_URL and cfg.SUPABASE_KEY):
            logger.debug("Supabase backup skipped — credentials not configured.")
            return False

        try:
            session = await self._get_session()
            url = f"{cfg.SUPABASE_URL.rstrip('/')}/rest/v1/{cfg.SUPABASE_TABLE}"
            headers = {
                "apikey": cfg.SUPABASE_KEY,
                "Authorization": f"Bearer {cfg.SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
            row = {
                "deal_id": deal_id,
                "backup_type": "basic",
                "data": {
                    "timestamp": ts,
                    "index": index_num,
                    "address": address,
                    "asset": asset,
                    "sender_id": sender_id,
                    "receiver_id": receiver_id,
                },
            }
            async with session.post(url, json=row, headers=headers) as resp:
                if resp.status in (200, 201):
                    logger.debug(f"Supabase backup done for {deal_id}")
                    return True
                body = await resp.text()
                logger.error(f"Supabase backup HTTP {resp.status}: {body[:200]}")
                return False
        except asyncio.TimeoutError:
            logger.error(f"Supabase backup timeout for {deal_id}")
            return False
        except Exception as e:
            logger.error(f"Supabase backup failed: {e}")
            return False

    async def _save_to_redis(
        self,
        deal_id: str,
        ts: str,
        index_num: int,
        address: str,
        asset: str,
        sender_id: int = None,
        receiver_id: int = None,
    ) -> bool:
        cfg = BackupConfig
        if not (cfg.UPSTASH_REDIS_URL and cfg.UPSTASH_REDIS_TOKEN):
            logger.debug("Redis backup skipped — credentials not configured.")
            return False

        try:
            session = await self._get_session()
            value = json.dumps({
                "deal_id": deal_id,
                "backup_type": "basic",
                "timestamp": ts,
                "index": index_num,
                "address": address,
                "asset": asset,
                "sender_id": sender_id,
                "receiver_id": receiver_id,
            })
            async with session.post(
                cfg.UPSTASH_REDIS_URL.rstrip("/"),
                json=["SET", f"escrow:backup:basic:{deal_id}", value],
                headers={"Authorization": f"Bearer {cfg.UPSTASH_REDIS_TOKEN}"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("result") == "OK":
                        logger.debug(f"Redis backup done for {deal_id}")
                        return True
                body = await resp.text()
                logger.error(f"Redis backup HTTP {resp.status}: {body[:200]}")
                return False
        except asyncio.TimeoutError:
            logger.error(f"Redis backup timeout for {deal_id}")
            return False
        except Exception as e:
            logger.error(f"Redis backup failed: {e}")
            return False

    async def save_extended_backup(
        self,
        deal_id: str,
        sender_info: dict,
        receiver_info: dict,
        deal_amount: dict,
        crypto_address: str,
        index_num: int,
        timestamp: Optional[str] = None,
    ) -> bool:
        async with self._detail_lock:
            try:
                deal_dir = DETAIL_BACKUP_DIR / deal_id
                deal_dir.mkdir(parents=True, exist_ok=True)

                ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

                data = {
                    "deal_id": deal_id,
                    "created_at": ts,
                    "finalized_at": None,
                    "status": "IN_PROGRESS",
                    "sender": sender_info,
                    "seller": receiver_info,
                    "deal_amount": deal_amount,
                    "crypto_address": crypto_address,
                    "index": index_num,
                    "transcript_file": None,
                    "cloud_uploads": {},
                }

                json_path = deal_dir / f"{deal_id}.json"
                async with aiofiles.open(json_path, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(data, indent=2, ensure_ascii=False))

                logger.info(f"Extended backup created: {deal_id}")
                return True

            except Exception as e:
                logger.error(f"save_extended_backup failed: {e}")
                return False

    async def update_extended_backup(
        self,
        deal_id: str,
        updates: dict,
    ) -> bool:
        async with self._detail_lock:
            try:
                deal_dir = DETAIL_BACKUP_DIR / deal_id
                json_path = deal_dir / f"{deal_id}.json"

                if not json_path.exists():
                    logger.warning(f"Extended backup not found: {json_path}")
                    return False

                async with aiofiles.open(json_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                    data = json.loads(content) if content.strip() else {}

                data.update(updates)

                async with aiofiles.open(json_path, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(data, indent=2, ensure_ascii=False))

                logger.debug(f"Extended backup updated: {deal_id}")
                return True

            except Exception as e:
                logger.error(f"update_extended_backup failed: {e}")
                return False

    async def get_extended_backup(self, deal_id: str) -> Optional[dict]:
        try:
            json_path = DETAIL_BACKUP_DIR / deal_id / f"{deal_id}.json"
            if not json_path.exists():
                return None

            async with aiofiles.open(json_path, "r", encoding="utf-8") as f:
                content = await f.read()
                return json.loads(content) if content.strip() else None

        except Exception as e:
            logger.error(f"get_extended_backup failed: {e}")
            return None

    async def _upload_extended_to_supabase(self, deal_id: str) -> bool:
        cfg = BackupConfig
        if not (cfg.SUPABASE_URL and cfg.SUPABASE_KEY):
            return False

        try:
            json_path = DETAIL_BACKUP_DIR / deal_id / f"{deal_id}.json"
            if not json_path.exists():
                return False

            data = json.loads(json_path.read_text(encoding="utf-8"))
            session = await self._get_session()
            url = f"{cfg.SUPABASE_URL.rstrip('/')}/rest/v1/{cfg.SUPABASE_TABLE}"
            headers = {
                "apikey": cfg.SUPABASE_KEY,
                "Authorization": f"Bearer {cfg.SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
            row = {"deal_id": deal_id, "backup_type": "detailed", "data": data}
            async with session.post(url, json=row, headers=headers) as resp:
                if resp.status in (200, 201):
                    logger.info(f"Supabase extended backup done for {deal_id}")
                    return True
                body = await resp.text()
                logger.error(f"Supabase extended backup HTTP {resp.status}: {body[:200]}")
                return False
        except Exception as e:
            logger.error(f"Supabase extended backup failed: {e}")
            return False

    async def _upload_extended_to_redis(self, deal_id: str) -> bool:
        cfg = BackupConfig
        if not (cfg.UPSTASH_REDIS_URL and cfg.UPSTASH_REDIS_TOKEN):
            return False

        try:
            json_path = DETAIL_BACKUP_DIR / deal_id / f"{deal_id}.json"
            if not json_path.exists():
                return False

            content = json_path.read_text(encoding="utf-8")
            session = await self._get_session()
            async with session.post(
                cfg.UPSTASH_REDIS_URL.rstrip("/"),
                json=["SET", f"escrow:backup:detailed:{deal_id}", content],
                headers={"Authorization": f"Bearer {cfg.UPSTASH_REDIS_TOKEN}"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("result") == "OK":
                        logger.info(f"Redis extended backup done for {deal_id}")
                        return True
                body = await resp.text()
                logger.error(f"Redis extended backup HTTP {resp.status}: {body[:200]}")
                return False
        except Exception as e:
            logger.error(f"Redis extended backup failed: {e}")
            return False

    async def upload_extended_to_clouds(self, deal_id: str) -> dict:
        config = _load_cloud_config()
        storages = config.get("cloud_storages", [])

        deal_dir = DETAIL_BACKUP_DIR / deal_id
        if not deal_dir.exists():
            logger.warning(f"Deal folder not found: {deal_dir}")
            return {}

        files_to_upload = [f for f in deal_dir.iterdir() if f.is_file()]

        results = {}

        # --- S3 / R2 storages (from detail_backup/config.json) ---
        for storage in storages:
            name = storage.get("name", "unknown")
            enabled = storage.get("enabled", False)

            if not enabled:
                results[name] = {"uploaded": False, "reason": "disabled"}
                continue

            try:
                client = await self._init_custom_s3_client(storage)
                if client is None:
                    results[name] = {"uploaded": False, "reason": "client_init_failed"}
                    continue

                uploaded_files = []
                for file_path in files_to_upload:
                    remote_key = f"detail_backup/{deal_id}/{file_path.name}"
                    await asyncio.to_thread(
                        client.put_object,
                        Bucket=storage["bucket"],
                        Key=remote_key,
                        Body=file_path.read_bytes(),
                    )
                    uploaded_files.append(remote_key)

                results[name] = {
                    "uploaded": True,
                    "files": uploaded_files,
                    "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                }
                logger.info(f"Uploaded {deal_id} to {name} ({len(uploaded_files)} files)")

            except Exception as e:
                logger.error(f"Upload to {name} failed: {e}")
                results[name] = {"uploaded": False, "reason": str(e)}

        # --- Supabase + Upstash Redis (env-driven) ---
        try:
            supabase_ok = await self._upload_extended_to_supabase(deal_id)
            results["supabase"] = {
                "uploaded": supabase_ok,
                "reason": "" if supabase_ok else "failed_or_not_configured",
            }
        except Exception as e:
            logger.error(f"Supabase extended upload error: {e}")
            results["supabase"] = {"uploaded": False, "reason": str(e)}

        try:
            redis_ok = await self._upload_extended_to_redis(deal_id)
            results["redis"] = {
                "uploaded": redis_ok,
                "reason": "" if redis_ok else "failed_or_not_configured",
            }
        except Exception as e:
            logger.error(f"Redis extended upload error: {e}")
            results["redis"] = {"uploaded": False, "reason": str(e)}

        await self.update_extended_backup(deal_id, {"cloud_uploads": results})
        return results

    async def _init_custom_s3_client(self, storage: dict):
        name = storage.get("name", "unknown")
        endpoint = storage.get("endpoint", "").strip()
        access_key = storage.get("access_key_id", "").strip()
        secret_key = storage.get("secret_access_key", "").strip()
        region = storage.get("region", "auto")

        if not (endpoint and access_key and secret_key):
            return None

        cache_key = f"custom|{name}|{endpoint}|{access_key}"
        if cache_key in self._s3_clients:
            return self._s3_clients[cache_key]

        try:
            import boto3
            from botocore.config import Config as BotoConfig

            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                config=BotoConfig(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=10,
                    read_timeout=10,
                ),
            )
            self._s3_clients[cache_key] = client
            return client

        except Exception as e:
            logger.error(f"Custom S3 client init failed for {name}: {e}")
            return None

    async def verify_backup(self, deal_id: str) -> bool:
        from Database import DatabaseManager

        try:
            entry = await DatabaseManager.get_backup_entry(deal_id)
            if not entry:
                return False

            locations = {}
            if entry.backup_locations:
                try:
                    locations = json.loads(entry.backup_locations)
                except json.JSONDecodeError:
                    locations = {}

            success = sum(1 for v in locations.values() if v)
            return success >= BackupConfig.MIN_BACKUP_SUCCESS

        except Exception as e:
            logger.error(f"verify_backup error: {e}")
            return False

    async def get_stats(self) -> dict:
        from Database import DatabaseManager
        return await DatabaseManager.get_backup_stats()


_backup_manager_instance: Optional[BackupManager] = None


def get_backup_manager() -> BackupManager:
    global _backup_manager_instance
    if _backup_manager_instance is None:
        _backup_manager_instance = BackupManager()
    return _backup_manager_instance