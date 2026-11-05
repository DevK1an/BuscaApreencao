"""Histórico local de varreduras e ofertas; valores monetários em centavos."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time


class PriceHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS price_scans (
                id INTEGER PRIMARY KEY, product_id TEXT NOT NULL,
                seen_at REAL NOT NULL, payload TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS scans_product ON price_scans(product_id,id)")
            db.commit()

    def record(self, product_id, offers, coverage, selected=None, query="", note=""):
        payload = {
            "offers": offers, "coverage": coverage, "selected": selected,
            "query": query, "note": note, "criterion": "advertised_unit_price",
        }
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute(
                "INSERT INTO price_scans(product_id,seen_at,payload) VALUES(?,?,?)",
                (product_id, time.time(), json.dumps(payload, ensure_ascii=False)),
            )
            db.commit()

    def clear(self, product_id):
        with closing(sqlite3.connect(self.path,timeout=10)) as db:
            db.execute("DELETE FROM price_scans WHERE product_id=?",(product_id,))
            db.commit()

    def get(self, product_id, limit=100, query=None):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            rows = db.execute(
                "SELECT id,seen_at,payload FROM price_scans WHERE product_id=? ORDER BY id DESC LIMIT ?",
                (product_id, max(1, min(int(limit), 200))),
            ).fetchall()
        def decoded(text):
            data=json.loads(text)
            if query:
                from offer_matching import matches
                original=data["offers"]
                data["offers"]=[offer for offer in original if matches(query,offer.get("title",""))]
                selected=data.get("selected")
                if selected and not any(
                    selected.get("key")==offer.get("key") and selected.get("marketplace")==offer.get("marketplace")
                    for offer in data["offers"]
                ):
                    data["selected"]=None
            return data
        scans = []
        for scan_id, seen_at, text in reversed(rows):
            data = decoded(text)
            values = [o["unit_cents"] for o in data["offers"] if isinstance(o.get("unit_cents"), int) and o["unit_cents"] > 0]
            scans.append({
                "id":scan_id, "seen_at":seen_at,
                "min_unit_cents":min(values) if values else None,
                "offer_count":len(data["offers"]), "complete":all(
                    source.get("status") in ("ok", "empty") for source in data["coverage"].values()
                ),
            })
        latest = decoded(rows[0][2]) if rows else None
        if latest:
            latest["seen_at"] = rows[0][1]
        return {"latest": latest, "series": scans, "scan_count": len(scans)}


def record_direct(settings, quantity, price, title=None, marketplace="amazon", url=None):
    import os
    target = os.environ.get("COMPRAHUB_HISTORY_FILE")
    product_id = os.environ.get("COMPRAHUB_PRODUCT_ID")
    if not target or not product_id:
        return
    offer = {
        "key":settings.asin, "marketplace":marketplace,
        "title":title or os.environ.get("COMPRAHUB_PRODUCT_NAME", settings.asin),
        "url":url or settings.product_url, "unit_cents":price,
        "shipping_cents":None, "shipping_note":"Frete a confirmar no checkout",
    }
    PriceHistory(target).record(
        product_id, [offer], {marketplace:{"status":"ok","pages":1,"results":1}},
        query=offer["title"], note="Preço anunciado da página monitorada; frete validado no checkout.",
    )
