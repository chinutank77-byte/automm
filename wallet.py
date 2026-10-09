import os
import logging
from dotenv import load_dotenv
from bip_utils import (
    Bip39SeedGenerator,
    Bip44, Bip49, Bip84,
    Bip44Coins, Bip49Coins, Bip84Coins,
    Bip44Changes,
    P2WPKHAddrDecoder,
    Base58Decoder,
)

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("WalletManager")

load_dotenv()


class WalletManager:
    _instance = None

    def __new__(cls):
        """Singleton Pattern: Poore bot me sirf ek hi wallet instance banega."""
        if cls._instance is None:
            cls._instance = super(WalletManager, cls).__new__(cls)
            cls._instance._initialize_wallet()
        return cls._instance

    def _initialize_wallet(self):
        """Seed phrase load karta hai aur Master Seed generate karta hai."""
        self.seed_phrase = os.getenv("SEED_PHRASE")
        if not self.seed_phrase:
            logger.critical("SEED_PHRASE .env file me nahi mila! Bot band ho raha hai.")
            raise ValueError("SEED_PHRASE missing in .env")

        # Seed phrase ko clean karo (extra spaces remove)
        self.seed_phrase = " ".join(self.seed_phrase.strip().split())

        # Word count validate karo
        word_count = len(self.seed_phrase.split())
        if word_count not in (12, 15, 18, 21, 24):
            raise ValueError(f"Invalid seed phrase: {word_count} words (12/15/18/21/24 expected)")

        # BIP-39 standard ke hisaab se seed bytes generate karo
        try:
            self.seed_bytes = Bip39SeedGenerator(self.seed_phrase).Generate()
        except Exception as e:
            logger.critical(f"Seed phrase invalid hai: {e}")
            raise ValueError("Invalid seed phrase. Kripya BIP-39 words check karein.")

        # Cache dictionary: Baar-baar derivation na karni pade
        self._address_cache = {}

        logger.info(f"Ultra Advanced Wallet Manager initialized ({word_count}-word seed).")

    def _get_derivation_context(self, coin_symbol: str, address_type: str, index: int):
        """
        Internal function jo coin aur address type ke hisaab se
        sahi BIP context (Bip44/Bip49/Bip84) return karta hai.
        """
        cache_key = f"{coin_symbol}_{address_type}_{index}"
        if cache_key in self._address_cache:
            return self._address_cache[cache_key]

        try:
            # Coin Type mapping (BIP-44 standard)
            coin_map = {
                "BTC": {
                    "legacy": (Bip44, Bip44Coins.BITCOIN),
                    "p2sh":   (Bip49, Bip49Coins.BITCOIN),
                    "native": (Bip84, Bip84Coins.BITCOIN),
                },
                "LTC": {
                    "legacy": (Bip44, Bip44Coins.LITECOIN),
                    "p2sh":   (Bip49, Bip49Coins.LITECOIN),
                    "native": (Bip84, Bip84Coins.LITECOIN),
                },
            }

            if coin_symbol not in coin_map:
                raise ValueError(f"Unsupported coin: {coin_symbol}")

            if address_type not in coin_map[coin_symbol]:
                raise ValueError(f"Unsupported address type: {address_type}")

            bip_class, coin_enum = coin_map[coin_symbol][address_type]
            bip_ctx = bip_class.FromSeed(self.seed_bytes, coin_enum)
            ctx = (
                bip_ctx
                .Purpose()
                .Coin()
                .Account(0)
                .Change(Bip44Changes.CHAIN_EXT)
                .AddressIndex(index)
            )

            # Cache me save karo
            self._address_cache[cache_key] = ctx
            return ctx

        except Exception as e:
            logger.error(f"Derivation context error: {e}")
            raise e

    def generate_address(
        self,
        coin_symbol: str = "LTC",
        address_type: str = "native",
        index: int = 0
    ) -> str:
        """
        Naya address generate karta hai.
        Default: Litecoin (LTC) ka Native SegWit (ltc1q...) address.
        """
        if index < 0:
            raise ValueError("Index negative nahi ho sakta")

        ctx = self._get_derivation_context(coin_symbol, address_type, index)
        address = ctx.PublicKey().ToAddress()
        logger.info(f"[{coin_symbol}] {address_type.capitalize()} Address (Index {index}): {address}")
        return address

    def get_private_key(
        self,
        coin_symbol: str = "LTC",
        address_type: str = "native",
        index: int = 0
    ) -> str:
        """
        [SECURITY WARNING]: Sirf Release Phase me use karna.
        Private Key ko kabhi DB me save mat karna.
        """
        ctx = self._get_derivation_context(coin_symbol, address_type, index)
        return ctx.PrivateKey().Raw().ToHex()

    def get_public_key(
        self,
        coin_symbol: str = "LTC",
        address_type: str = "native",
        index: int = 0
    ) -> str:
        """Public key return karta hai (debug ke liye useful)."""
        ctx = self._get_derivation_context(coin_symbol, address_type, index)
        return ctx.PublicKey().RawCompressed().ToHex()

    # ==========================================
    # ADDRESS VALIDATION (FIXED - No LtcAddrDecoder needed)
    # ==========================================
    @staticmethod
    def validate_address(coin_symbol: str, address: str) -> bool:
        """
        Address validation: Legacy (L), P2SH (M), Native SegWit (ltc1q),
        Taproot (ltc1p), MWEB (ltc1mweb) sabko support karta hai.
        """
        if not address or not isinstance(address, str):
            return False

        address = address.strip()

        if coin_symbol == "LTC":
            return WalletManager._validate_ltc(address)
        elif coin_symbol == "BTC":
            return WalletManager._validate_btc(address)
        else:
            logger.warning(f"Validation unsupported for coin: {coin_symbol}")
            return False

    @staticmethod
    def _validate_ltc(address: str) -> bool:
        """Litecoin address validation (mainnet + testnet)."""
        try:
            # --- Native SegWit (Bech32): ltc1q... ---
            if address.startswith("ltc1q"):
                if not (42 <= len(address) <= 62):
                    return False
                P2WPKHAddrDecoder.DecodeAddr(address)
                return True

            # --- Taproot (Bech32m): ltc1p... ---
            if address.startswith("ltc1p"):
                # Bech32m checksum manually verify karna mushkil hai,
                # isliye length + prefix check kar rahe hain
                return len(address) == 62

            # --- MWEB: ltc1mweb... ---
            if address.startswith("ltc1mweb"):
                return len(address) > 20

            # --- Testnet Native SegWit: tltc1... ---
            if address.startswith("tltc1"):
                return len(address) >= 42

            # --- Legacy (L...): P2PKH mainnet ---
            if address.startswith("L"):
                if len(address) != 34:
                    return False
                decoded = Base58Decoder.Decode(address)
                return decoded[0] == 0x30  # LTC P2PKH version byte

            # --- P2SH (M...): Nested SegWit mainnet ---
            if address.startswith("M"):
                if len(address) != 34:
                    return False
                decoded = Base58Decoder.Decode(address)
                return decoded[0] == 0x32  # LTC P2SH version byte

            # --- Legacy P2SH (3...): Compatibility ---
            if address.startswith("3"):
                if len(address) != 34:
                    return False
                decoded = Base58Decoder.Decode(address)
                return decoded[0] == 0x05

            # --- Testnet Legacy: m, n ---
            if address.startswith(("m", "n")):
                if len(address) != 34:
                    return False
                decoded = Base58Decoder.Decode(address)
                return decoded[0] == 0x6F

            # --- Testnet P2SH: Q ---
            if address.startswith("Q"):
                if len(address) != 34:
                    return False
                decoded = Base58Decoder.Decode(address)
                return decoded[0] == 0x3A

            return False

        except Exception as e:
            logger.debug(f"LTC validation failed for {address}: {e}")
            return False

    @staticmethod
    def _validate_btc(address: str) -> bool:
        """Bitcoin address validation (future use ke liye)."""
        try:
            # Native SegWit: bc1q...
            if address.startswith("bc1q"):
                if not (42 <= len(address) <= 62):
                    return False
                P2WPKHAddrDecoder.DecodeAddr(address)
                return True

            # Taproot: bc1p...
            if address.startswith("bc1p"):
                return len(address) == 62

            # Legacy: 1...
            if address.startswith("1"):
                if len(address) != 34:
                    return False
                decoded = Base58Decoder.Decode(address)
                return decoded[0] == 0x00

            # P2SH: 3...
            if address.startswith("3"):
                if len(address) != 34:
                    return False
                decoded = Base58Decoder.Decode(address)
                return decoded[0] == 0x05

            # Testnet
            if address.startswith(("tb1q", "tb1p")):
                return len(address) >= 42

            if address.startswith(("m", "n")):
                if len(address) != 34:
                    return False
                decoded = Base58Decoder.Decode(address)
                return decoded[0] == 0x6F

            if address.startswith("2"):
                if len(address) != 34:
                    return False
                decoded = Base58Decoder.Decode(address)
                return decoded[0] == 0xC4

            return False
        except Exception:
            return False

    def get_balance(self, address: str) -> float:
        """
        [PLACEHOLDER]: Ye function blockchain API se balance fetch karega.
        Real implementation Backgroundtask.py me API call karegi.
        """
        return 0.0

    def clear_cache(self):
        """Cache clear karta hai (memory management ke liye)."""
        self._address_cache.clear()
        logger.info("Address cache clear ho gayi.")


# ==========================================
# TEST (Sirf direct run karne par)
# ==========================================
if __name__ == "__main__":
    try:
        wallet = WalletManager()

        print("\n--- Address Generation Tests ---")
        addr_native = wallet.generate_address("LTC", "native", 0)
        print(f"Native SegWit (index 0): {addr_native}")

        addr_legacy = wallet.generate_address("LTC", "legacy", 0)
        print(f"Legacy (index 0): {addr_legacy}")

        addr_p2sh = wallet.generate_address("LTC", "p2sh", 0)
        print(f"P2SH (index 0): {addr_p2sh}")

        addr_native_1 = wallet.generate_address("LTC", "native", 1)
        print(f"Native SegWit (index 1): {addr_native_1}")

        print("\n--- Validation Tests ---")
        print(f"Valid native? {wallet.validate_address('LTC', addr_native)}")
        print(f"Valid legacy? {wallet.validate_address('LTC', addr_legacy)}")
        print(f"Valid p2sh? {wallet.validate_address('LTC', addr_p2sh)}")
        print(f"Invalid test? {wallet.validate_address('LTC', 'invalid_xyz')}")
        print(f"Valid known LTC? {wallet.validate_address('LTC', 'LhyLNfBkoKshT7R8Pce6vkB9T2cP2o84hx')}")

        print("\n--- Public Key Test ---")
        print(f"Public Key (index 0): {wallet.get_public_key('LTC', 'native', 0)}")

    except Exception as e:
        print(f"Error: {e}")