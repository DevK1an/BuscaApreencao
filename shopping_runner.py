"""Google Shopping: Amazon prepara checkout; demais lojas apenas abrem o produto."""
from urllib.parse import urlsplit, urlunsplit
from playwright.sync_api import Error, sync_playwright

from app_paths import browser_environment, data_root
from google_shopping import collect, needs_access
from merchant_rules import excluded_store, merchant_url, google_url
from policy import CannotVerify, Settings, brl
from runtime import BotStopped, check_stop, emit_status, wait
from status import show_status


def open_store_product(page,offer,quantity):
    """Nenhum botão de compra, cupom ou quantidade é acionado fora da Amazon."""
    detail=(f"{offer.get('merchant_name') or urlsplit(page.url).hostname} · "
            f"Preço anunciado: {brl(offer['unit_cents'])}. Quantidade desejada: {quantity}. "
            "Confira preço, estoque, frete e condições na loja.")
    location=urlsplit(page.url)
    print("[Loja] Produto aberto para conferência pessoal: "+urlunsplit((location.scheme,location.netloc,location.path,"","")),flush=True)
    page.bring_to_front()
    show_status(page,"Produto aberto na loja",detail)
    emit_status("Produto aberto na loja",detail,state="opened")
    while not page.is_closed():
        try:
            wait(page,1000)
        except Error:
            if page.is_closed():
                break
            raise


def install_navigation_filter(page,excluded):
    def guard(route):
        request=route.request
        if request.is_navigation_request() and request.frame==page.main_frame:
            try:
                url=merchant_url(request.url)
                if excluded_store("",url,excluded):
                    print("[Exclusão] Navegação bloqueada para loja excluída.",flush=True)
                    route.abort()
                    return
            except ValueError:
                route.abort()
                return
        route.continue_()
    page.route("**/*",guard)


def dispatch_candidates(page,context,settings,quantity,result,history=None,product_id=None):
    """Abre a menor oferta permitida. Nunca prepara carrinho de outra loja."""
    from amazon import product_price, needs_human
    from bot import wait_for_login
    from hub_store import normalize_url
    from offer_matching import matches
    from coupons import has_available_coupon
    from price_tracker import prepare_amazon, hold

    excluded=settings.excluded_stores
    queue=[dict(o) for o in result.offers if not excluded_store(o.get("merchant_name",""),o["url"],excluded)]
    while queue:
        check_stop()
        queue.sort(key=lambda o:(o["unit_cents"],o.get("merchant_name",""),o["key"]))
        offer=queue.pop(0)
        if offer["unit_cents"]>settings.max_unit_cents and not (offer["marketplace"]=="amazon" and offer.get("coupon_available")):
            continue
        try:
            response=page.goto(merchant_url(offer["url"]),wait_until="domcontentloaded",timeout=30000)
            if response and response.status>=400:
                raise CannotVerify(f"A loja retornou HTTP {response.status}.")
            final_url=merchant_url(page.url)
            if excluded_store(offer.get("merchant_name",""),final_url,excluded):
                print("[Exclusão] Destino final descartado.",flush=True)
                continue
            host=urlsplit(final_url).hostname
            if google_url(final_url):
                raise CannotVerify("O link não saiu do Google para o produto da loja.")
            if host not in {"amazon.com.br","www.amazon.com.br"}:
                if history:
                    history.record(product_id,result.offers,result.coverage,offer,settings.query,
                                   "Produto aberto na menor oferta anunciada disponível; compra e total serão conferidos pessoalmente na loja.")
                open_store_product(page,offer,quantity)
                return True
            key,url=normalize_url(final_url)
            if needs_human(page):
                wait_for_login(page)
                page.goto(url,wait_until="domcontentloaded")
            title=page.locator("#productTitle")
            title.wait_for(state="visible",timeout=12000)
            if not matches(settings.query,title.inner_text()):
                raise CannotVerify("O produto da Amazon não corresponde ao nome/modelo procurado.")
            live_price=product_price(page,key)
            if live_price!=offer["unit_cents"]:
                for original in result.offers:
                    if original["key"]==offer["key"] and original["url"]==offer["url"]:
                        original.update(unit_cents=live_price,price_source="amazon_verified")
            if live_price>settings.max_unit_cents and not has_available_coupon(page,"amazon"):
                print("[Amazon] Preço atual acima do teto; oferta descartada.",flush=True)
                continue
            if live_price!=offer["unit_cents"]:
                offer.update(unit_cents=live_price,key=key,url=url)
                # Um preço atualizado não pode ultrapassar outra oferta conhecida mais barata.
                if queue and live_price>min(o["unit_cents"] for o in queue):
                    queue.append(offer)
                    continue
            offer.update(key=key,url=url,marketplace="amazon")
        except (ValueError,Error) as error:
            print("[Oferta ignorada] "+str(error).splitlines()[0][:240],flush=True)
            continue
        if history:
            history.record(product_id,result.offers,result.coverage,offer,settings.query,
                           "Amazon: preço e título conferidos; cupons, recusa de seguro e total com frete serão validados no preparo.")
        emit_status("Preparando seu pedido","Amazon selecionada. Conferindo cupons e carrinho; a confirmação final será sua.",state="preparing")
        # A partir daqui, falhas não acionam outra inclusão ou outra loja.
        try:
            prepare_amazon(page,context,Settings(key,settings.max_unit_cents,settings.interval_seconds),quantity)
        except (CannotVerify,Error) as error:
            if not page.is_closed():
                hold(page,str(error).splitlines()[0])
        return True
    return False


def run(settings,quantity):
    from price_tracker import history_store
    browser_environment()
    history,product_id=history_store()
    check_stop()
    with sync_playwright() as pw:
        context=pw.chromium.launch_persistent_context(str(data_root()/".amazon-profile"),headless=False,locale="pt-BR")
        context.set_default_timeout(12000)
        page=context.pages[0] if context.pages else context.new_page()
        install_navigation_filter(page,settings.excluded_stores)
        try:
            while not page.is_closed():
                check_stop()
                def progress(detail):
                    print(detail,flush=True)
                    show_status(page,"Rastreando preços",detail)
                result=collect(page,settings.query,settings.pages_per_store,settings.excluded_stores,progress)
                info=result.coverage["google_shopping"]
                note=info["detail"]+" Seleção por preço anunciado; frete e condições precisam ser confirmados."
                if history:
                    history.record(product_id,result.offers,result.coverage,None,settings.query,note)
                print("[Cobertura] "+note,flush=True)
                incomplete=info["status"] not in ("ok","empty")
                can_select=bool(result.offers) and (not incomplete or settings.allow_partial)
                if can_select:
                    if incomplete:
                        for offer in result.offers:
                            offer["selection_partial"]=True
                    if dispatch_candidates(page,context,settings,quantity,result,history,product_id):
                        return
                if needs_access(page):
                    show_status(page,"Aguardando acesso ao Google","Conclua a verificação ou o consentimento nesta janela. A busca continua automaticamente.","warning")
                    while not page.is_closed() and needs_access(page):
                        wait(page,1000)
                    continue
                message=note
                if result.offers:
                    message+=f" Menor anúncio permitido: {brl(min(o['unit_cents'] for o in result.offers))}; teto por unidade: {brl(settings.max_unit_cents)}."
                show_status(page,"Comparação incompleta" if incomplete else "Monitorando o produto",message,"warning" if incomplete else "info")
                wait(page,settings.interval_seconds*1000)
        except BotStopped:
            emit_status("Monitoramento parado",state="stopped")
        except Error as error:
            if not page.is_closed():
                show_status(page,"Preparação interrompida",str(error).splitlines()[0],"warning")
        finally:
            context.close()


def run_external_link(raw,quantity):
    browser_environment()
    url=merchant_url(raw["url"])
    check_stop()
    with sync_playwright() as pw:
        context=pw.chromium.launch_persistent_context(str(data_root()/".amazon-profile"),headless=False,locale="pt-BR")
        page=context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url,wait_until="domcontentloaded",timeout=30000)
            # Em links externos não houve leitura de preço: evita apresentar o teto como preço.
            detail=f"Quantidade desejada: {quantity}. Confira preço, estoque, frete e condições pessoalmente na loja."
            show_status(page,"Produto aberto na loja",detail)
            emit_status("Produto aberto na loja",detail,state="opened")
            page.bring_to_front()
            while not page.is_closed():
                wait(page,1000)
        except BotStopped:
            emit_status("Monitoramento parado",state="stopped")
        except Error as error:
            if not page.is_closed():
                emit_status("Preparação interrompida",str(error).splitlines()[0],"warning",state="error")
        finally:
            context.close()
