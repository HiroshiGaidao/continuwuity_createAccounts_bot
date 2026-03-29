# database.py
import duckdb
import threading
import os
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "/app/data/bot_database.db")

_db_lock = threading.Lock()
_db_initialized = False

def _row_to_dict(cursor, row):
    """Конвертирует строку результата DuckDB в dict"""
    if row is None:
        return None
    return {desc[0]: value for desc, value in zip(cursor.description, row)}

def _rows_to_dicts(cursor, rows):
    """Конвертирует список строк в список dict"""
    if rows is None:
        return []
    return [_row_to_dict(cursor, row) for row in rows]

@contextmanager
def get_db_connection():
    """Контекстный менеджер для подключения к DuckDB"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = duckdb.connect(DB_PATH)
    conn.execute("SET threads TO 1")
    try:
        yield conn
    except Exception as e:
        try:
            conn.rollback()
        except:
            pass
        raise e
    finally:
        conn.close()

def init_db():
    """Инициализация таблиц"""
    global _db_initialized
    if _db_initialized:
        return
    
    with _db_lock:
        with get_db_connection() as conn:
            # Таблица пользователей
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    tg_chat_id BIGINT PRIMARY KEY,
                    tg_username VARCHAR,
                    first_name VARCHAR,
                    registered_at TIMESTAMP DEFAULT now(),
                    last_activity TIMESTAMP DEFAULT now(),
                    status VARCHAR DEFAULT 'active',
                    registration_count INTEGER DEFAULT 0
                )
            """)
            
            # Последовательности для авто-инкремента
            conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_matrix_accounts_id START 1")
            conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_action_logs_id START 1")
            
            # Таблица аккаунтов Matrix
            conn.execute("""
                CREATE TABLE IF NOT EXISTS matrix_accounts (
                    id BIGINT PRIMARY KEY DEFAULT nextval('seq_matrix_accounts_id'),
                    tg_chat_id BIGINT,
                    matrix_username VARCHAR,
                    matrix_full_id VARCHAR,
                    created_at TIMESTAMP DEFAULT now(),
                    status VARCHAR DEFAULT 'active'
                )
            """)
            
            # Таблица логов
            conn.execute("""
                CREATE TABLE IF NOT EXISTS action_logs (
                    id BIGINT PRIMARY KEY DEFAULT nextval('seq_action_logs_id'),
                    tg_chat_id BIGINT,
                    action VARCHAR,
                    details VARCHAR,
                    timestamp TIMESTAMP DEFAULT now()
                )
            """)
            
            # Индексы
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_tg ON matrix_accounts(tg_chat_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_matrix ON matrix_accounts(matrix_username)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_chat_id ON users(tg_chat_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON action_logs(timestamp)")
            
            _db_initialized = True

def add_user(tg_chat_id: int, tg_username: str, first_name: str):
    """Добавляет пользователя или обновляет активность"""
    with _db_lock:
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO users (tg_chat_id, tg_username, first_name, last_activity)
                VALUES (?, ?, ?, now())
                ON CONFLICT (tg_chat_id) DO UPDATE SET
                    tg_username = excluded.tg_username,
                    first_name = excluded.first_name,
                    last_activity = now()
            """, (tg_chat_id, tg_username, first_name))

def get_user(tg_chat_id: int) -> dict:
    """Получает данные пользователя"""
    with get_db_connection() as conn:
        cursor = conn.execute("SELECT * FROM users WHERE tg_chat_id = ?", (tg_chat_id,))
        row = cursor.fetchone()
        return _row_to_dict(cursor, row)

def ban_user(tg_chat_id: int, reason: str = ""):
    """Банит пользователя"""
    with _db_lock:
        with get_db_connection() as conn:
            conn.execute("UPDATE users SET status = 'banned' WHERE tg_chat_id = ?", (tg_chat_id,))
            log_action_internal(conn, tg_chat_id, "ban", reason)

def unban_user(tg_chat_id: int):
    """Разбанивает пользователя"""
    with _db_lock:
        with get_db_connection() as conn:
            conn.execute("UPDATE users SET status = 'active' WHERE tg_chat_id = ?", (tg_chat_id,))
            log_action_internal(conn, tg_chat_id, "unban", "")

def is_user_banned(tg_chat_id: int) -> bool:
    """Проверяет, забанен ли пользователь"""
    user = get_user(tg_chat_id)
    return user and user.get("status") == "banned"

def add_matrix_account(tg_chat_id: int, matrix_username: str, matrix_full_id: str):
    """Добавляет аккаунт Matrix пользователю"""
    with _db_lock:
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO matrix_accounts (tg_chat_id, matrix_username, matrix_full_id)
                VALUES (?, ?, ?)
            """, (tg_chat_id, matrix_username, matrix_full_id))
            conn.execute("""
                UPDATE users SET registration_count = registration_count + 1
                WHERE tg_chat_id = ?
            """, (tg_chat_id,))
            log_action_internal(conn, tg_chat_id, "register", f"Created {matrix_full_id}")

def get_user_accounts(tg_chat_id: int) -> list:
    """Получает все аккаунты Matrix пользователя"""
    with get_db_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM matrix_accounts WHERE tg_chat_id = ? AND status = 'active'",
            (tg_chat_id,)
        )
        rows = cursor.fetchall()
        return _rows_to_dicts(cursor, rows)

def hard_delete_matrix_account(tg_chat_id: int, matrix_username: str):
    """Полное удаление аккаунта из БД"""
    with _db_lock:
        with get_db_connection() as conn:
            conn.execute("""
                DELETE FROM matrix_accounts
                WHERE tg_chat_id = ? AND matrix_username = ?
            """, (tg_chat_id, matrix_username))
            conn.execute("""
                UPDATE users SET registration_count = registration_count - 1
                WHERE tg_chat_id = ? AND registration_count > 0
            """, (tg_chat_id,))
            log_action_internal(conn, tg_chat_id, "hard_delete", f"Removed {matrix_username}")

def get_all_users(limit: int = 50, offset: int = 0) -> list:
    """Получает список всех пользователей"""
    with get_db_connection() as conn:
        cursor = conn.execute("""
            SELECT * FROM users ORDER BY registered_at DESC LIMIT ? OFFSET ?
        """, (limit, offset))
        rows = cursor.fetchall()
        return _rows_to_dicts(cursor, rows)

def get_stats() -> dict:
    """Получает статистику"""
    with get_db_connection() as conn:
        cursor = conn.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'active') as active,
                COUNT(*) FILTER (WHERE status = 'banned') as banned
            FROM users
        """)
        row = cursor.fetchone()
        stats = _row_to_dict(cursor, row)
        
        cursor = conn.execute("SELECT COUNT(*) FROM matrix_accounts WHERE status = 'active'")
        accounts = cursor.fetchone()[0]
        
        return {
            "total": stats.get("total", 0),
            "active": stats.get("active", 0),
            "banned": stats.get("banned", 0),
            "accounts": accounts
        }

def log_action(tg_chat_id: int, action: str, details: str):
    """Публичный метод логирования"""
    with _db_lock:
        with get_db_connection() as conn:
            log_action_internal(conn, tg_chat_id, action, details)

def log_action_internal(conn, tg_chat_id: int, action: str, details: str):
    """Внутренний метод логирования"""
    conn.execute("""
        INSERT INTO action_logs (tg_chat_id, action, details)
        VALUES (?, ?, ?)
    """, (tg_chat_id, action, details))

def get_logs(days: int = 7, limit: int = 100) -> list:
    """Получает логи за последние N дней"""
    with _db_lock:
        with get_db_connection() as conn:
            cursor = conn.execute(f"""
                SELECT * FROM action_logs 
                WHERE timestamp >= now() - INTERVAL '{days}' DAY
                ORDER BY timestamp DESC
                LIMIT {limit}
            """)
            rows = cursor.fetchall()
            return _rows_to_dicts(cursor, rows)

def search_users(query: str) -> list:
    """Поиск пользователей по username или имени"""
    with get_db_connection() as conn:
        cursor = conn.execute("""
            SELECT * FROM users 
            WHERE tg_username ILIKE ? OR first_name ILIKE ?
            LIMIT 20
        """, (f"%{query}%", f"%{query}%"))
        rows = cursor.fetchall()
        return _rows_to_dicts(cursor, rows)

# Инициализация при импорте
init_db()