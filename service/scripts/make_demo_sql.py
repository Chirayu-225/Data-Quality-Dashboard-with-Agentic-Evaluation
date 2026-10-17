"""Writes demo_data/demo_mysql.sql: the three demo tables with real primary
keys and declared foreign keys (orphan rows included on purpose), plus a
read-only user, for MySQL / MariaDB. Load with:

    mysql -u root -p < demo_data/demo_mysql.sql

Then connect the app to host 127.0.0.1, database scorecard_demo,
user scorecard_ro, password readonly_demo_pw (demo credentials only).
"""
import csv
from pathlib import Path

root = Path(__file__).resolve().parent.parent / "demo_data"


def q(v):
    return "NULL" if v == "" else "'" + v.replace("\\", "\\\\").replace("'", "''") + "'"


def rows(table, cols):
    with open(root / f"{table}.csv", newline="", encoding="utf-8") as f:
        data = list(csv.DictReader(f))
    vals = ",\n  ".join("(" + ", ".join(q(r[c]) for c in cols) + ")" for r in data)
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES\n  {vals};\n"


sql = """CREATE DATABASE IF NOT EXISTS scorecard_demo CHARACTER SET utf8mb4;
USE scorecard_demo;
SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS opportunities;
DROP TABLE IF EXISTS contacts;
DROP TABLE IF EXISTS accounts;
CREATE TABLE accounts (
  id INT PRIMARY KEY, company VARCHAR(120), industry VARCHAR(80), country_code VARCHAR(8),
  annual_revenue DECIMAL(16,2), website VARCHAR(200)) ENGINE=InnoDB;
CREATE TABLE contacts (
  id INT PRIMARY KEY, account_id INT, first_name VARCHAR(80), last_name VARCHAR(80),
  email VARCHAR(160), phone VARCHAR(40), title VARCHAR(80),
  FOREIGN KEY (account_id) REFERENCES accounts(id)) ENGINE=InnoDB;
CREATE TABLE opportunities (
  id INT PRIMARY KEY, account_id INT, contact_id INT, stage VARCHAR(40),
  amount DECIMAL(16,2), close_date VARCHAR(20),
  FOREIGN KEY (account_id) REFERENCES accounts(id),
  FOREIGN KEY (contact_id) REFERENCES contacts(id)) ENGINE=InnoDB;
"""
sql += rows("accounts", ["id", "company", "industry", "country_code", "annual_revenue", "website"])
sql += rows("contacts", ["id", "account_id", "first_name", "last_name", "email", "phone", "title"])
sql += rows("opportunities", ["id", "account_id", "contact_id", "stage", "amount", "close_date"])
sql += """SET FOREIGN_KEY_CHECKS = 1;
CREATE USER IF NOT EXISTS 'scorecard_ro'@'%' IDENTIFIED BY 'readonly_demo_pw';
CREATE USER IF NOT EXISTS 'scorecard_ro'@'localhost' IDENTIFIED BY 'readonly_demo_pw';
GRANT SELECT ON scorecard_demo.* TO 'scorecard_ro'@'%';
GRANT SELECT ON scorecard_demo.* TO 'scorecard_ro'@'localhost';
FLUSH PRIVILEGES;
"""
(root / "demo_mysql.sql").write_text(sql, encoding="utf-8")
print("wrote", root / "demo_mysql.sql")
