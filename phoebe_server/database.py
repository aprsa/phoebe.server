import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager
from .config import config

logger = logging.getLogger(__name__)

# Global database path
_db_path: Path | None = None


def init_database():
    """Initialize the database with schema and WAL mode."""
    global _db_path
    _db_path = Path(config.database.path)

    # Create directory if it doesn't exist
    _db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_db() as db:
        # Enable WAL mode for better concurrency
        db.execute("PRAGMA journal_mode=WAL")

        # Create tables
        db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                destroyed_at REAL,
                last_activity REAL NOT NULL,
                port INTEGER NOT NULL,
                client_ip TEXT,
                user_agent TEXT,
                termination_reason TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS session_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                memory_used_mb REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS session_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                command_name TEXT NOT NULL,
                success INTEGER NOT NULL,
                execution_time_ms REAL,
                error_message TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS session_user_info (
                session_id TEXT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                email TEXT,
                updated_at REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            )
        """)

        # Create indexes
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_created_at
            ON sessions (created_at)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_status
            ON sessions (status)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_commands_session_id
            ON session_commands (session_id)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_metrics_session_id
            ON session_metrics (session_id)
        """)

        db.commit()
        logger.info(f"Database initialized at {_db_path}")


@contextmanager
def get_db():
    """Get a database connection context manager."""
    if _db_path is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")

    conn = sqlite3.connect(str(_db_path), timeout=10.0)
    try:
        yield conn
    finally:
        conn.close()


def should_log_command(command_name: str) -> bool:
    """Check if a command should be logged based on configuration."""
    exclude = [c.strip() for c in config.database.log_exclude_commands.split(",") if c.strip()]
    include = [c.strip() for c in config.database.log_include_commands.split(",") if c.strip()]

    # If include list is specified, only log those
    if include:
        return command_name in include

    # Otherwise, log everything except excluded
    return command_name not in exclude


def log_session_created(session_id: str, created_at: float, port: int, client_ip: str | None = None, user_agent: str | None = None):
    """Log a new session creation."""
    try:
        with get_db() as db:
            db.execute("""
                INSERT INTO sessions
                (session_id, created_at, last_activity, port, client_ip, user_agent, status)
                VALUES (?, ?, ?, ?, ?, ?, 'active')
            """, (session_id, created_at, created_at, port, client_ip, user_agent))
            db.commit()
            logger.debug(f"Logged session creation: {session_id}")
    except Exception as e:
        logger.error(f"Failed to log session creation: {e}")


def log_session_destroyed(session_id: str, destroyed_at: float, termination_reason: str):
    """Log session destruction."""
    try:
        with get_db() as db:
            db.execute("""
                UPDATE sessions
                SET destroyed_at = ?, termination_reason = ?, status = 'terminated'
                WHERE session_id = ?
            """, (destroyed_at, termination_reason, session_id))
            db.commit()
            logger.debug(f"Logged session destruction: {session_id}")
    except Exception as e:
        logger.error(f"Failed to log session destruction: {e}")


def log_session_activity(session_id: str, last_activity: float):
    """Update session last activity timestamp."""
    try:
        with get_db() as db:
            db.execute("""
                UPDATE sessions
                SET last_activity = ?
                WHERE session_id = ?
            """, (last_activity, session_id))
            db.commit()
    except Exception as e:
        logger.error(f"Failed to log session activity: {e}")


def log_session_metric(session_id: str, timestamp: float, memory_used_mb: float):
    """Log a session resource metric snapshot."""
    try:
        with get_db() as db:
            db.execute("""
                INSERT INTO session_metrics
                (session_id, timestamp, memory_used_mb)
                VALUES (?, ?, ?)
            """, (session_id, timestamp, memory_used_mb))
            db.commit()
    except Exception as e:
        logger.error(f"Failed to log session metric: {e}")


def log_command_execution(session_id: str, timestamp: float, command_name: str, success: bool, execution_time_ms: float | None = None, error_message: str | None = None):
    """Log a command execution."""
    if not should_log_command(command_name):
        return

    try:
        with get_db() as db:
            db.execute("""
                INSERT INTO session_commands
                (session_id, timestamp, command_name, success, execution_time_ms, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session_id, timestamp, command_name, int(success), execution_time_ms, error_message))
            db.commit()
    except Exception as e:
        logger.error(f"Failed to log command execution: {e}")


def log_user_info_update(session_id: str, first_name: str, last_name: str, email: str, updated_at: float):
    """Log or update user information for a session."""
    try:
        with get_db() as db:
            db.execute("""
                INSERT OR REPLACE INTO session_user_info
                (session_id, first_name, last_name, email, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, first_name, last_name, email, updated_at))
            db.commit()
    except Exception as e:
        logger.error(f"Failed to log user info update: {e}")
