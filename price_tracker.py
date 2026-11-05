"""Busca repetida nas lojas, escolha automática e histórico local."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from app_paths import browser_environment, data_root
from market_search import STORES, collect, eligible_offers, select_lowest
from offer_matching import STOP, matches, tokens
from policy import CannotVerify, OfferUnavailable, Settings, positive_int, brl
from price_history import PriceHistory, record_direct
from runtime import BotStopped, check_stop, emit_status, wait
from status import show_status
from coupons import activate_coupons, has_available_coupon


@dataclass(frozen=True)
class SearchSettings:
    query: str
    max_unit_cents: int
    interval_seconds: int
    marketplaces: tuple
    pages_per_store: int
    allow_partial: bool = False
    search_provider: str = "marketplaces"
    excluded_stores: tuple = ()

    @classmethod
    def load(cls, data):
        raw_query = data.get("query", "")
        if not isinstance(raw_query,str) or any(ord(char)<32 for char in raw_query):
            raise ValueError("Nome de busca inválido.")
        query = raw_query.strip()
        if len(query) > 180 or len(tokens(query) - STOP) < 2:
            raise ValueError("Informe o nome com marca e modelo, com pelo menos duas palavras.")
        provider=data.get("search_provider","marketplaces")
        if provider not in ("marketplaces","google_shopping"):
            raise ValueError("Origem de busca inválida.")
        if provider=="google_shopping":
            from merchant_rules import DEFAULT_EXCLUDED, normalize_exclusions
            excluded=normalize_exclusions(data.get("excluded_stores",list(DEFAULT_EXCLUDED)))
            interval=positive_int(data.get("interval_seconds",120),"Intervalo",86400)
            if interval<60:
                raise ValueError("Use pelo menos 60 segundos entre buscas.")
            partial=data.get("allow_partial",True)
            if type(partial) is not bool:
                raise ValueError("A opção de comparação parcial deve ser verdadeira ou falsa.")
            return cls(query,positive_int(data["max_unit_cents"],"Limite",100_000_000),interval,
                       ("google_shopping",),positive_int(data.get("pages_per_store",30),"Rodadas",50),partial,provider,excluded)
        stores = data.get("marketplaces", list(STORES))
        if not isinstance(stores, list) or not stores or any(not isinstance(s,str) or s not in STORES for s in stores) or len(stores) != len(set(stores)):
            raise ValueError("Escolha Amazon e/ou Mercado Livre.")
        interval = positive_int(data.get("interval_seconds",120), "Intervalo",86400)
        if interval < 60:
            raise ValueError("Use pelo menos 60 segundos entre buscas.")
        allow_partial=data.get("allow_partial",False)
        if type(allow_partial) is not bool:
            raise ValueError("A opção de comparação parcial deve ser verdadeira ou falsa.")
        return cls(
            query, positive_int(data["max_unit_cents"],"Limite",100_000_000), interval,
            tuple(stores), positive_int(data.get("pages_per_store",3),"Páginas por loja",10), allow_partial,
        )


def history_store():
    path = os.environ.get("COMPRAHUB_HISTORY_FILE")
    product_id = os.environ.get("COMPRAHUB_PRODUCT_ID")
    if not path or not product_id:
        return None, None
    return PriceHistory(path), product_id


def prepare_amazon(page, context, settings, quantity):
    from amazon import CART_URL, add_to_cart, cart_items, open_checkout, require_exact_cart, set_quantity, wait_for_cart_add
    from bot import final_review, wait_for_login
    set_quantity(page, quantity)
    if activate_coupons(page, "amazon"):
        from amazon import product_price
        if product_price(page, settings.asin) > settings.max_unit_cents and not has_available_coupon(page, "amazon"):
            raise CannotVerify("A oferta mudou de preço durante a ativação do cupom. Confira a Amazon.")
        set_quantity(page, quantity)
    cart = context.new_page()
    try:
        cart.goto(CART_URL, wait_until="domcontentloaded")
        from amazon import needs_human
        if needs_human(cart):
            cart.bring_to_front()
            wait_for_login(cart)
            cart.goto(CART_URL, wait_until="domcontentloaded")
        existing = cart_items(cart)
        if existing and existing != [(settings.asin,quantity)]:
            raise CannotVerify("O carrinho contém outros itens ou quantidade diferente. Ajuste-o na Amazon.")
    finally:
        cart.close()
    if not existing:
        check_stop()
        add_to_cart(page)
        wait_for_cart_add(page, settings.asin, quantity)
    page.goto(CART_URL, wait_until="domcontentloaded")
    require_exact_cart(page,settings.asin,quantity)
    activate_coupons(page, "amazon")
    require_exact_cart(page,settings.asin,quantity)
    check_stop()
    open_checkout(page)
    final_review(page,settings,quantity)


def hold(page, message):
    from playwright.sync_api import Error
    from bot import log
    log("Preparação interrompida: " + message)
    while not page.is_closed():
        try:
            show_status(page,"Preparação interrompida",message,"warning")
            wait(page,1000)
        except Error:
            break


def run(settings, quantity):
    if settings.search_provider=="google_shopping":
        from shopping_runner import run as run_google
        return run_google(settings,quantity)
    from playwright.sync_api import Error, sync_playwright
    from bot import log
    from hub_store import normalize_url
    from amazon import product_price as amazon_price
    import mercadolivre as ml

    browser_environment()
    history, product_id = history_store()
    check_stop()
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(data_root()/".amazon-profile"), headless=False, locale="pt-BR")
        context.set_default_timeout(12000)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            while not page.is_closed():
                check_stop()
                def progress(detail):
                    log(detail)
                    show_status(page,"Rastreando preços",detail)
                    emit_status("Rastreando preços",detail,state="monitoring")
                result = collect(page,settings.query,settings.marketplaces,settings.pages_per_store,progress)
                candidates=eligible_offers(result,settings.allow_partial)
                selected=select_lowest(result,settings.allow_partial)
                incomplete = [name for name,info in result.coverage.items() if info["status"] not in ("ok","empty")]
                note=(
                    "Comparação parcial: menor preço anunciado entre as ofertas legíveis disponíveis. Frete será validado no checkout."
                    if incomplete and selected else
                    "Busca parcial; escolha automática suspensa. Confira o acesso às lojas."
                    if incomplete else
                    "Menor preço anunciado entre os resultados compatíveis desta varredura. Frete será validado no checkout."
                )
                for store,info in result.coverage.items():
                    log(f"{store}: {info.get('results',0)} anúncios, {info.get('compatible',0)} compatíveis, {info.get('priced',0)} preços, {info.get('unreadable',0)} ilegíveis. {info.get('detail','')}")
                if history:
                    history.record(product_id,result.offers,result.coverage,selected,settings.query,note)
                if incomplete:
                    detail="; ".join(f"{name}: {result.coverage[name]['detail']}" for name in incomplete)
                    log("Comparação parcial: "+detail)
                if not selected:
                    if incomplete:
                        show_status(page,"Comparação incompleta",note+" "+detail,"warning")
                        emit_status("Comparação incompleta",note+" "+detail,"warning",state="needs_attention")
                    else:
                        summary="; ".join(
                            f"{name}: {info.get('results',0)} anúncios lidos, {info.get('compatible',0)} compatíveis, {info.get('unreadable',0)} sem preço"
                            for name,info in result.coverage.items()
                        )
                        show_status(page,"Monitorando o produto","Nenhuma oferta compatível nesta varredura. "+summary)
                        log("Nenhuma oferta compatível: "+summary)
                elif selected["unit_cents"]>settings.max_unit_cents and not any(offer.get("coupon_available") for offer in candidates):
                    message=f"Menor preço anunciado legível: {brl(selected['unit_cents'])}; seu teto por unidade: {brl(settings.max_unit_cents)}."
                    if incomplete:
                        message+=" Comparação parcial: usando somente as ofertas disponíveis."
                    show_status(page,"Monitorando o produto",message)
                    log(message)
                else:
                    # Só tenta ofertas do mesmo conjunto validado, em ordem crescente de preço anunciado.
                    for offer in candidates:
                        if offer["unit_cents"] > settings.max_unit_cents and not offer.get("coupon_available"):
                            continue
                        check_stop()
                        page.goto(offer["url"],wait_until="domcontentloaded")
                        try:
                            key,url = normalize_url(page.url)
                            if key != offer["key"]:
                                raise CannotVerify("A loja alterou a identificação do anúncio selecionado.")
                            title_locator = page.locator("#productTitle" if offer["marketplace"]=="amazon" else "h1.ui-pdp-title")
                            title_locator.first.wait_for(state="visible",timeout=12000)
                            if not matches(settings.query,title_locator.first.inner_text()):
                                raise OfferUnavailable("O título do anúncio deixou de corresponder à busca.")
                            price = amazon_price(page,key) if offer["marketplace"]=="amazon" else ml.product_price(page)
                            if price != offer["unit_cents"] or (price > settings.max_unit_cents and not has_available_coupon(page, offer["marketplace"])):
                                log("A oferta mudou de preço; será comparada novamente na próxima varredura.")
                                continue
                        except (CannotVerify,Error) as error:
                            if isinstance(error,OfferUnavailable) and offer.get("offer_kind") == "other_sellers" and offer["marketplace"] == "amazon":
                                from bot import final_review
                                log("A menor oferta aparece em outros vendedores. Selecione a oferta na Amazon; o total continuará sendo validado.")
                                final_review(page,Settings(offer["key"],settings.max_unit_cents,settings.interval_seconds),quantity)
                                return
                            log(f"Oferta ignorada antes de adicionar: {str(error).splitlines()[0]}")
                            continue
                        if history:
                            history.record(product_id,result.offers,result.coverage,offer,settings.query,
                                ("Comparação parcial. " if incomplete else "")+"Oferta selecionada após conferência do preço e título. Frete será validado no checkout.")
                        emit_status("Preparando seu pedido",f"Oferta selecionada: {offer['marketplace']} · {brl(price)} por unidade."+(" Cupom anunciado: total com frete e desconto ainda precisa ser validado." if offer.get("coupon_available") else "")+(" Comparação parcial: outras consultas falharam." if incomplete else ""),state="preparing")
                        # Depois de entrar na preparação, nenhuma falha repete a adição.
                        target = Settings(key,settings.max_unit_cents,settings.interval_seconds) if offer["marketplace"]=="amazon" else ml.MLSettings(key,settings.max_unit_cents,settings.interval_seconds,url)
                        try:
                            if offer["marketplace"]=="amazon":
                                prepare_amazon(page,context,target,quantity)
                            else:
                                from shopping_runner import open_store_product
                                open_store_product(page,offer,quantity)
                        except (CannotVerify,Error) as error:
                            hold(page,str(error).splitlines()[0])
                        return
                wait(page,settings.interval_seconds*1000)
        except BotStopped:
            log("Rastreamento parado pelo hub.")
            emit_status("Monitoramento parado",state="stopped")
        except Error as error:
            if not page.is_closed():
                hold(page,str(error).splitlines()[0])
        finally:
            context.close()


def run_ml_link(raw, quantity):
    from shopping_runner import run_external_link
    return run_external_link(raw,quantity)
