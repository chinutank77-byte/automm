import os
import logging
from dotenv import load_dotenv
from bip_utils import (
    Bip39SeedGenerator, 
    Bip44, 
    Bip44Coins, 
    Bip44Changes,
    LtcAddrDecoder,
    LtcAddrEncoder,
    P2WPKHAddrDecoder # Native SegWit (Bech32) ke liye
)

# Logging setup (Debugging ke liye)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# .env file load karo
load_dotenv()

class WalletManager:
    _instance = None

    def __new__(cls):
        # Singleton Pattern: Taaki poore bot me sirf ek hi wallet instance bane
        if cls._instance is None:
            cls._instance = super(WalletManager, cls).__new__(cls)
            cls._instance._initialize_wallet()
        return cls._instance

    def _initialize_wallet(self):
        """Seed phrase load karta hai aur Master Wallet initialize karta hai."""
        self.seed_phrase = os.getenv("SEED_PHRASE")
        if not self.seed_phrase:
            logger.critical("SEED_PHRASE .env file me nahi mila! Bot band ho raha hai.")
            raise ValueError("SEED_PHRASE missing in .env")
        
        # Seed phrase se Master Seed generate karo
        self.seed_bytes = Bip39SeedGenerator(self.seed_phrase).Generate()
        logger.info("Master Wallet successfully initialize ho gaya.")

    def generate_address(self, index: int) -> str:
        """
        Deal Index ke hisaab se naya Litecoin (LTC) Native SegWit (Bech32) address generate karta hai.
        Derivation Path: m/44'/2'/0'/0/{index}
        """
        try:
            # Litecoin (Coin Type: 2) ka BIP-44 context banao
            bip44_mst_ctx = Bip44.FromSeed(self.seed_bytes, Bip44Coins.LITECOIN)
            
            # Derivation Path: Purpose(44) -> Coin(2) -> Account(0) -> Change(0) -> Address Index
            bip44_acc_ctx = bip44_mst_ctx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(index)
            
            # Address generate karo (Native SegWit - ltc1q...)
            address = bip44_acc_ctx.PublicKey().ToAddress()
            logger.info(f"Index {index} ke liye naya address generate hua: {address}")
            return address
        except Exception as e:
            logger.error(f"Address generation failed for index {index}: {e}")
            raise e

    def get_private_key(self, index: int) -> str:
        """
        [SECURITY WARNING]: Ye function sirf Release Phase me use hoga.
        Isse us index ki Private Key derive hoti hai. Ise kabhi database me save mat karna.
        """
        try:
            bip44_mst_ctx = Bip44.FromSeed(self.seed_bytes, Bip44Coins.LITECOIN)
            bip44_acc_ctx = bip44_mst_ctx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(index)
            
            # Private Key (WIF format me) return karo
            private_key_wif = bip44_acc_ctx.PrivateKey().Raw().ToHex()
            return private_key_wif
        except Exception as e:
            logger.error(f"Private key derivation failed for index {index}: {e}")
            raise e

    @staticmethod
    def validate_ltc_address(address: str) -> bool:
        """
        User ka diya hua Litecoin address valid hai ya nahi, ye check karta hai.
        Ye Legacy (L), P2SH (M), aur Native SegWit (ltc1q) sabko support karta hai.
        """
        try:
            # Pehle check karo ki ye Bech32 (Native SegWit) hai kya
            if address.startswith("ltc1"):
                P2WPKHAddrDecoder.DecodeAddr(address)
                return True
            else:
                # Legacy ya P2SH address ke liye
                LtcAddrDecoder.DecodeAddr(address)
                return True
        except Exception:
            logger.warning(f"Invalid Litecoin address detected: {address}")
            return False

# Test karne ke liye (Jab directly run karo tabhi chalega)
if __name__ == "__main__":
    wallet = WalletManager()
    # Maan lo pehla deal hai (index 0)
    addr_0 = wallet.generate_address(0)
    print(f"Deal 1 Address: {addr_0}")
    
    # Dusra deal (index 1)
    addr_1 = wallet.generate_address(1)
    print(f"Deal 2 Address: {addr_1}")
    
    # Address validation test
    print(f"Valid Address? {wallet.validate_ltc_address(addr_0)}")
    print(f"Valid Address? {wallet.validate_ltc_address('LhyLNfBkoKshT7R8Pce6vkB9T2cP2o84hx')}")
    print(f"Valid Address? {wallet.validate_ltc_address('invalid_address_123')}")