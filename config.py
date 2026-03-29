# config.py
import os

CONFIG = {
    # Telegram
    "TG_BOT_TOKEN": os.getenv("TG_BOT_TOKEN"),
    "ADMIN_CHAT_ID": int(os.getenv("ADMIN_CHAT_ID", "0")),
    
    # Matrix
    "MATRIX_SERVER_URL": os.getenv("MATRIX_SERVER_URL", "https://matrix.yourdomain.com"),
    "MATRIX_DOMAIN": os.getenv("MATRIX_DOMAIN", "yourdomain.com"),
    "MATRIX_BOT_USER": os.getenv("MATRIX_BOT_USER", "regbot"),
    "MATRIX_BOT_PASSWORD": os.getenv("MATRIX_BOT_PASSWORD"),
    "MATRIX_ADMIN_ROOM_ID": os.getenv("MATRIX_ADMIN_ROOM_ID"),
    "MATRIX_STORE_PATH": os.getenv("MATRIX_STORE_PATH", "/app/matrix_store"),
    
    # Database
    "DB_PATH": os.getenv("DB_PATH", "/app/data/bot_database.db"),
    
    # Logging
    "LOG_PATH": os.getenv("LOG_PATH", "/app/logs"),
    "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO"),
    "LOG_MAX_BYTES": int(os.getenv("LOG_MAX_BYTES", "10485760")),
    "LOG_BACKUP_COUNT": int(os.getenv("LOG_BACKUP_COUNT", "7")),
}