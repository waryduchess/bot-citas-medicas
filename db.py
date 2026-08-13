import os
import sqlite3

DB_LOCAL = os.getenv("DATABASE_PATH", "citas.db")

def get_conn():
    """Conexión a la BD. Si hay credenciales de Turso configuradas usa libSQL remoto,
    si no, SQLite local (desarrollo)."""
    turso_url = os.getenv("TURSO_DATABASE_URL")
    turso_token = os.getenv("TURSO_AUTH_TOKEN")
    if turso_url and turso_token:
        import libsql
        return libsql.connect(database=turso_url, auth_token=turso_token)
    return sqlite3.connect(DB_LOCAL)
