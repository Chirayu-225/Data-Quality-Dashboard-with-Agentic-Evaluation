"""Builds demo_data/demo.db (SQLite) from the three demo CSVs, with real
primary keys and declared foreign keys, so the 'Connect a source' flow can be
demoed with zero database setup. SQLite does not enforce the foreign keys
by default, which is what lets the planted orphan rows exist.

    python scripts/make_demo_db.py
"""
import csv
import sqlite3
from pathlib import Path

root = Path(__file__).resolve().parent.parent / "demo_data"
db = root / "demo.db"
db.unlink(missing_ok=True)
con = sqlite3.connect(db)
con.executescript(
    """
    CREATE TABLE accounts (
      id INTEGER PRIMARY KEY, company TEXT, industry TEXT, country_code TEXT,
      annual_revenue REAL, website TEXT);
    CREATE TABLE contacts (
      id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id),
      first_name TEXT, last_name TEXT, email TEXT, phone TEXT, title TEXT);
    CREATE TABLE opportunities (
      id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id),
      contact_id INTEGER REFERENCES contacts(id), stage TEXT, amount REAL, close_date TEXT);
    """
)


def load(table, cols):
    with open(root / f"{table}.csv", newline="", encoding="utf-8") as f:
        rows = [
            [None if (r[c] == "") else r[c] for c in cols]
            for r in csv.DictReader(f)
        ]
    con.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)


load("accounts", ["id", "company", "industry", "country_code", "annual_revenue", "website"])
load("contacts", ["id", "account_id", "first_name", "last_name", "email", "phone", "title"])
load("opportunities", ["id", "account_id", "contact_id", "stage", "amount", "close_date"])
con.commit()
con.close()
print("wrote", db)
