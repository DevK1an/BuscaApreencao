"""Produtos persistidos localmente, com links normalizados da Amazon Brasil."""
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import threading
import uuid
from urllib.parse import unquote, urlsplit

from policy import Settings, positive_int
from atomic_files import replace_file

ASIN_PATH = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([a-z0-9]{10})(?:[/?]|$)", re.I)


class Conflict(ValueError):
    pass


def normalize_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Informe um link válido da Amazon ou do Mercado Livre.")
    raw = value.strip()
    if raw.startswith(("www.amazon.com.br/", "amazon.com.br/", "www.mercadolivre.com.br/", "mercadolivre.com.br/", "produto.mercadolivre.com.br/")):
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError()
        path = unquote(parsed.path)
        if parsed.hostname in ("www.amazon.com.br","amazon.com.br"):
            match = ASIN_PATH.search(path)
            if not match:
                raise ValueError()
            asin = match[1].upper()
            return asin, f"https://www.amazon.com.br/dp/{asin}"
        if parsed.hostname in ("www.mercadolivre.com.br","mercadolivre.com.br","produto.mercadolivre.com.br"):
            match = re.search(r"/(?:p/)?(MLB)-?([0-9]{5,})(?:[-/]|$)",path,re.I)
            if not match:
                raise ValueError()
            key = "MLB" + match[2]
            from urllib.parse import parse_qs, urlencode, quote, urlunsplit
            filters = parse_qs(parsed.query).get("pdp_filters", [])
            query = ""
            if filters and re.fullmatch(r"item_id:MLB[0-9]{5,}",filters[0],re.I):
                query = urlencode({"pdp_filters":filters[0]})
                key = filters[0].split(":")[1].upper()
            host = "www.mercadolivre.com.br" if parsed.hostname == "mercadolivre.com.br" else parsed.hostname
            return key, urlunsplit(("https",host,quote(path,safe="/-_.~"),query,""))
        raise ValueError()
    except ValueError:
        raise ValueError("Use um link completo de produto: amazon.com.br/dp/ASIN ou Mercado Livre (MLB).") from None


def price_cents(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("Informe um preço máximo válido.")
    raw = str(value).strip().replace("R$", "").replace(" ", "").replace("\xa0", "")
    if "," in raw:
        if not re.fullmatch(r"(?:\d{1,3}(?:\.\d{3})+|\d+),\d{1,2}", raw):
            raise ValueError("Use um valor como 4.500,00.")
        raw = raw.replace(".", "").replace(",", ".")
    elif not re.fullmatch(r"\d+(?:\.\d{1,2})?", raw):
        raise ValueError("Use um valor como 4500 ou 4.500,00.")
    try:
        cents = Decimal(raw) * 100
    except InvalidOperation:
        raise ValueError("Preço inválido.") from None
    return positive_int(int(cents), "Preço em centavos", 100_000_000)


def validate_product(data):
    if not isinstance(data, dict):
        raise ValueError("Dados do produto inválidos.")
    mode = data.get("mode", "url")
    if mode not in ("url","search"):
        raise ValueError("Tipo de monitoramento inválido.")
    name = data.get("name", "")
    if not isinstance(name,str):
        raise ValueError("Nome inválido.")
    name = name.strip()
    if len(name) > 100 or any(ord(char) < 32 for char in name):
        raise ValueError("O nome deve ter até 100 caracteres.")
    quantity = positive_int(data.get("quantity"),"Quantidade")
    cents = price_cents(data.get("max_price","4500,00"))
    interval = positive_int(data.get("interval_seconds",120),"Intervalo",86400)
    if interval < 60:
        raise ValueError("Use pelo menos 60 segundos entre consultas.")
    common = {"quantity":quantity,"max_unit_cents":cents,"interval_seconds":interval}
    if mode == "search":
        import hashlib
        from price_tracker import SearchSettings
        from market_search import search_url
        settings = SearchSettings.load({**data,"allow_partial":data.get("allow_partial",True),"max_unit_cents":cents,"interval_seconds":interval})
        identity = settings.query.casefold() + "|" + "|".join(sorted(settings.marketplaces))
        if settings.search_provider=="google_shopping":
            identity += "|" + "|".join(sorted(value.casefold() for value in settings.excluded_stores))
        key = "BUSCA-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        if settings.search_provider=="google_shopping":
            from google_shopping import search_url as google_search_url
            search_link=google_search_url(settings.query)
        else:
            search_link=search_url(settings.marketplaces[0],settings.query)
        return {
            **common,"mode":"search","query":settings.query,"search_provider":settings.search_provider,
            **({"excluded_stores":list(settings.excluded_stores)} if settings.search_provider=="google_shopping" else {}),
            "name":name or settings.query[:100],"asin":key,
            "url":search_link,
            "marketplaces":list(settings.marketplaces),"pages_per_store":settings.pages_per_store,"allow_partial":settings.allow_partial,
        }
    try:
        asin,url = normalize_url(data.get("url"))
    except ValueError:
        from merchant_rules import merchant_url, offer_key
        url=merchant_url(data.get("url"))
        host=urlsplit(url).hostname
        if host in {"amazon.com.br","www.amazon.com.br","mercadolivre.com.br","www.mercadolivre.com.br","produto.mercadolivre.com.br"}:
            raise
        asin="LINK-"+offer_key(url)
        return {**common,"name":name or host,"url":url,"asin":asin,"marketplace":"external"}
    result = {**common,"name":name or f"Produto {asin}","url":url,"asin":asin}
    if asin.startswith("MLB"):
        result["marketplace"] = "mercadolivre"
    return result


class ProductStore:
    def __init__(self, path, seed_config=None):
        self.path = Path(path)
        self.lock = threading.RLock()
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("O arquivo de produtos está em um formato inválido.")
            self.products = []
            ids = set()
            asins = set()
            for saved in data:
                item = validate_product({
                    **saved,
                    **({"search_provider":"google_shopping","pages_per_store":30}
                       if saved.get("mode")=="search" and not saved.get("search_provider") else {}),
                    "max_price": f"{saved['max_unit_cents'] // 100}.{saved['max_unit_cents'] % 100:02d}"
                })
                if not re.fullmatch(r"[a-f0-9]{32}", saved.get("id", "")):
                    raise ValueError("Identificador de produto inválido.")
                if saved["id"] in ids or (item["asin"] in asins and item.get("search_provider")!="google_shopping"):
                    raise ValueError("Há produtos duplicados no arquivo.")
                ids.add(saved["id"])
                asins.add(item["asin"])
                self.products.append({"id": saved["id"], **item})
        else:
            self.products = []
            if seed_config and Path(seed_config).exists():
                settings = Settings.load(Path(seed_config))
                self.products.append({
                    "id": uuid.uuid4().hex, "name": "Meu primeiro produto",
                    "url": settings.product_url, "asin": settings.asin,
                    "quantity": 1, "max_unit_cents": settings.max_unit_cents,
                    "interval_seconds": settings.interval_seconds,
                })
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.products, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        replace_file(temporary,self.path)

    def list(self):
        with self.lock:
            return [dict(item) for item in self.products]

    def get(self, product_id):
        with self.lock:
            for item in self.products:
                if item["id"] == product_id:
                    return dict(item)
        raise KeyError("Produto não encontrado.")

    def save(self, data, product_id=None):
        validated = validate_product(data)
        with self.lock:
            if product_id:
                self.get(product_id)
            if validated.get("search_provider")!="google_shopping" and any(p["asin"] == validated["asin"] and p["id"] != product_id for p in self.products):
                raise Conflict("Este produto já está cadastrado. Use Editar para ajustar a quantidade.")
            item = {"id": product_id or uuid.uuid4().hex, **validated}
            previous = self.products
            self.products = [
                item if p["id"] == product_id else p for p in self.products
            ] if product_id else [*self.products, item]
            try:
                self._save()
            except OSError:
                self.products = previous
                raise
            return dict(item)

    def remove(self, product_id):
        with self.lock:
            self.get(product_id)
            previous = self.products
            self.products = [p for p in self.products if p["id"] != product_id]
            try:
                self._save()
            except OSError:
                self.products = previous
                raise
