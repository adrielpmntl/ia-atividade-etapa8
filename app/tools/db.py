"""Acesso compartilhado ao banco de dados PostgreSQL."""

import psycopg2

from app.config import DATABASE_URL


def get_conn():
    """Cria uma conexão com o banco de dados configurado."""
    return psycopg2.connect(DATABASE_URL)


def inicializar_schema_financeiro() -> None:
    """Cria as tabelas financeiras mínimas quando o banco ainda está vazio."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transaction_types (
                id INTEGER PRIMARY KEY,
                type VARCHAR(30) NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name VARCHAR(80) NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                amount NUMERIC(14, 2) NOT NULL CHECK (amount >= 0),
                type INTEGER NOT NULL REFERENCES transaction_types(id),
                category_id INTEGER REFERENCES categories(id),
                description TEXT,
                payment_method VARCHAR(80),
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                source_text TEXT NOT NULL
            );
            INSERT INTO transaction_types (id, type) VALUES
                (1, 'INCOME'), (2, 'EXPENSES'), (3, 'TRANSFER')
            ON CONFLICT (id) DO NOTHING;
            INSERT INTO categories (name) VALUES
                ('comida'), ('besteira'), ('estudo'), ('férias'),
                ('transporte'), ('moradia'), ('saúde'), ('lazer'),
                ('contas'), ('investimento'), ('presente'), ('outros')
            ON CONFLICT (name) DO NOTHING;
        """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
