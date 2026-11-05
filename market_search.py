"""Leitura dos resultados públicos de busca, sem contornar bloqueios das lojas."""
from dataclasses import dataclass
import re
from urllib.parse import quote, quote_plus, urljoin, urlsplit

from hub_store import normalize_url
from offer_matching import match_reason
from policy import CannotVerify, parse_brl
from runtime import check_stop, wait

STORES = ("amazon", "mercadolivre")
STORE_NAMES = {"amazon":"Amazon", "mercadolivre":"Mercado Livre"}


@dataclass
class SearchResult:
    offers: list
    coverage: dict


def search_url(marketplace, query):
    if marketplace == "amazon":
        return "https://www.amazon.com.br/s?k=" + quote_plus(query) + "&s=price-asc-rank"
    if marketplace == "mercadolivre":
        return "https://lista.mercadolivre.com.br/" + quote(query.replace(" ", "-"), safe="") + "_OrderId_PRICE"
    raise ValueError("Loja não reconhecida.")


def parse_amount(data, marketplace):
    text = (data.get("text") or "").strip()
    values = []
    if text:
        values.append(parse_brl(text))
    whole = re.sub(r"\s+", "", data.get("whole") or "").removesuffix(",")
    symbol = re.sub(r"\s+", "", data.get("symbol") or "")
    fraction = re.sub(r"\s+", "", data.get("fraction") or "")
    if whole or symbol or fraction:
        if marketplace == "mercadolivre" and whole and symbol and not fraction:
            # O componente andes-money-amount omite centavos quando são zero.
            fraction = "00"
        if symbol != "R$" or not re.fullmatch(r"(?:[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+)", whole) or not re.fullmatch(r"[0-9]{2}", fraction):
            raise CannotVerify("Preço do resultado incompleto.")
        values.append(parse_brl(f"R$ {whole},{fraction}"))
    if not values or len(set(values)) != 1 or values[0] <= 0:
        raise CannotVerify("Preço do resultado não reconhecido.")
    return values[0]


CARD_SCRIPT = r"""marketplace => {
    const amazon = marketplace === 'amazon';
    const roots = [...document.querySelectorAll(amazon ? '[data-component-type="s-search-result"][data-asin]' : '.poly-card, .ui-search-result__wrapper')];
    return roots.filter(el => el.getClientRects().length)
        .filter(el => amazon || !el.parentElement.closest('.poly-card, .ui-search-result__wrapper'))
        .slice(0,60).map(el => {
            const title = el.querySelector(amazon ? 'h2' : '.poly-component__title, .ui-search-item__title');
            const link = amazon ? (title?.closest('a') || el.querySelector('h2 a, a[href*="/dp/"]')) :
                el.querySelector('a.poly-component__title, a.ui-search-link, a[href*="/p/MLB"], a[href*="/MLB-"]');
            const price = el.querySelector(amazon ? '.a-price:not(.a-text-price)' :
                '.poly-price__current .andes-money-amount, .ui-search-price__second-line .andes-money-amount');
            const content = s => price?.querySelector(s)?.textContent || '';
            const body = el.innerText || '';
            const otherSellers = amazon && !price && /Nenhuma opção de compra em destaque/i.test(body) && /ofertas de novos/i.test(body);
            const unavailable = /atualmente indispon[ií]vel|sem estoque/i.test(body);
            return {
                title:title?.textContent?.trim() || '', unavailable, offer_kind:otherSellers ? 'other_sellers' : 'main',
                coupon_available:/\b(?:cupom|coupon)\b/i.test(body),
                url:link?.href || '', key:el.getAttribute('data-asin'),
                amount:{
                    text:amazon ? (otherSellers ? body : content('.a-offscreen')) : '',
                    symbol:content(amazon ? '.a-price-symbol' : '.andes-money-amount__currency-symbol'),
                    whole:content(amazon ? '.a-price-whole' : '.andes-money-amount__fraction'),
                    fraction:content(amazon ? '.a-price-fraction' : '.andes-money-amount__cents')
                },
                shipping_note:(el.querySelector(amazon ? '[data-cy="delivery-recipe"]' :
                    '.poly-component__shipping, .ui-search-item__shipping')?.textContent || '').trim().slice(0,200)
            };
        });
}"""


def read_cards(page, marketplace, query, diagnostics=None):
    raw = page.evaluate(CARD_SCRIPT, marketplace)
    offers = []
    unreadable = 0
    stats=diagnostics if diagnostics is not None else {}
    for card in raw:
        if card.get("unavailable"):
            stats["unavailable"]=stats.get("unavailable",0)+1
            continue
        reason=match_reason(query,card["title"])
        if reason:
            stats["filtered"]=stats.get("filtered",0)+1
            stats[reason]=stats.get(reason,0)+1
            continue
        stats["compatible"]=stats.get("compatible",0)+1
        try:
            key, url = normalize_url(card["url"])
            if (marketplace == "amazon") != (not key.startswith("MLB")):
                continue
            price = parse_amount(card["amount"], marketplace)
        except ValueError:
            unreadable += 1
            continue
        offers.append({
            "key":key, "marketplace":marketplace, "title":card["title"],
            "url":url, "unit_cents":price, "shipping_cents":None,
            "shipping_note":card["shipping_note"] or "Frete a confirmar no checkout",
            "offer_kind":card.get("offer_kind","main"),
            "coupon_available":bool(card.get("coupon_available",False)),
        })
    return offers, len(raw), unreadable


def blocked(page, response):
    if response and response.status in (401,403,429,503):
        return f"A loja retornou HTTP {response.status}. Abra a janela e resolva o acesso, se disponível."
    if page.locator('#captchacharacters, input[name="cvf_captcha_input"], #ap_email, input[name="user_id"], #captcha').count():
        return "A loja pediu login ou CAPTCHA. Resolva na janela do navegador."
    path = urlsplit(page.url).path.lower()
    if "/login" in path or "/registration" in path or "/ap/" in path or "captcha" in path:
        return "A loja pediu autenticação ou CAPTCHA."
    return None


def collect(page, query, marketplaces, max_pages=3, progress=None):
    offers = {}
    coverage = {}
    for marketplace in marketplaces:
        name = STORE_NAMES[marketplace]
        visited = set()
        current = search_url(marketplace, query)
        info = {"status":"ok", "pages":0, "results":0, "unreadable":0, "detail":""}
        coverage[marketplace] = info
        try:
            for number in range(max_pages):
                check_stop()
                if not current or current in visited:
                    break
                visited.add(current)
                if progress:
                    progress(f"Pesquisando {name}: página {number+1} de até {max_pages}.")
                response = page.goto(current, wait_until="domcontentloaded", timeout=30000)
                wait(page, 1200)
                reason = blocked(page, response)
                if reason:
                    info.update(status="blocked", detail=reason)
                    break
                diagnostics={}
                found, card_count, unreadable = read_cards(page,marketplace,query,diagnostics)
                for key in ("compatible","filtered","unavailable","not_console","different_model"):
                    info[key]=info.get(key,0)+diagnostics.get(key,0)
                info["pages"] += 1
                info["results"] += card_count
                info["unreadable"] += unreadable
                for offer in found:
                    key = (marketplace, offer["key"])
                    if key not in offers or offer["unit_cents"] < offers[key]["unit_cents"]:
                        offers[key] = offer
                if card_count == 0:
                    text = page.locator("body").inner_text().lower()
                    if any(t in text for t in ("não encontramos resultados", "nenhum resultado", "no results", "não há anúncios")):
                        info["status"] = "empty"
                    else:
                        info.update(status="unsupported", detail="A estrutura de resultados não foi reconhecida.")
                    break
                selector = '.s-pagination-next:not(.s-pagination-disabled)' if marketplace == "amazon" else 'a.andes-pagination__link[title="Seguinte"], a[aria-label="Seguinte"], a[title="Próxima"]'
                node = page.locator(selector)
                href = node.first.get_attribute("href") if node.count() else None
                current = urljoin(page.url, href) if href else None
                if current:
                    host = urlsplit(current).hostname
                    allowed = ("www.amazon.com.br",) if marketplace == "amazon" else ("lista.mercadolivre.com.br",)
                    if urlsplit(current).scheme != "https" or host not in allowed:
                        info.update(status="unsupported", detail="A próxima página saiu da busca reconhecida.")
                        break
            info["has_more"] = bool(current) and len(visited) >= max_pages
        except Exception as error:
            from runtime import BotStopped
            if isinstance(error, BotStopped):
                raise
            info.update(status="error", detail=str(error).splitlines()[0][:250])
        info["priced"]=sum(1 for offer in offers.values() if offer["marketplace"]==marketplace)
        if info["unreadable"] and info["status"]=="ok":
            info.update(status="incomplete",detail=f"{info['priced']} oferta(s) com preço; {info['unreadable']} anúncio(s) compatível(is) sem preço legível.")
        elif info["status"]=="ok":
            info["detail"]=f"{info['priced']} oferta(s) com preço; {info.get('filtered',0)} anúncio(s) de outro modelo/categoria descartado(s)."
    return SearchResult(sorted(offers.values(), key=lambda o:(o["unit_cents"],o["marketplace"],o["key"])), coverage)


def eligible_offers(result,allow_partial=False):
    good={"ok","empty","incomplete"} if allow_partial else {"ok","empty"}
    if not result.coverage:
        return []
    partial=any(info["status"] not in ("ok","empty") for info in result.coverage.values())
    if partial and not allow_partial:
        return []
    valid=[
        dict(offer,selection_partial=partial)
        if partial else offer
        for offer in result.offers
        if (
            result.coverage.get(offer["marketplace"],{}).get("status") in good
            or (
                allow_partial
                and result.coverage.get(offer["marketplace"],{}).get("status") in {"blocked","error","unsupported"}
                and result.coverage[offer["marketplace"]].get("pages",0)>0
                and result.coverage[offer["marketplace"]].get("priced",0)>0
            )
        )
    ]
    return sorted(valid,key=lambda offer:(offer["unit_cents"],offer["marketplace"],offer["key"]))


def select_lowest(result,allow_partial=False):
    candidates=eligible_offers(result,allow_partial)
    return candidates[0] if candidates else None
