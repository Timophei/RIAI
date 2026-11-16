import os
import sqlite3

DATA = os.environ.get("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA, "supply.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers(
  id INTEGER PRIMARY KEY, name TEXT, position_id INTEGER, request_id INTEGER,
  price REAL, selected INTEGER, comment TEXT, in_stock TEXT, vat TEXT, parent_id INTEGER,
  date_create TEXT, ts TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS i_off_pos ON offers(position_id);
CREATE INDEX IF NOT EXISTS i_off_req ON offers(request_id);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY, name TEXT, qty TEXT, unit TEXT, weight TEXT, purchase_id INTEGER,
  status TEXT, justification TEXT, date_create TEXT, ts TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY, title TEXT, company_id INTEGER, purchase_id INTEGER,
  created TEXT, updated TEXT, stage TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY, title TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def connect():
    os.makedirs(DATA, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def get_meta(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))
