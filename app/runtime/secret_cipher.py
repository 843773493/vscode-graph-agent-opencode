"""命名密钥使用的加密依赖，只在实际读写密文时加载。"""

from cryptography.fernet import Fernet

__all__ = ["Fernet"]
