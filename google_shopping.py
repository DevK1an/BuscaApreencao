"""Leitura do Google Shopping: cartões, todas as lojas e cobertura informada."""
import re
from urllib.parse import urlencode, urljoin, urlsplit
from playwright.sync_api import Error
from market_search import SearchResult
from merchant_rules import (
    GOOGLE_HOSTS, excluded_store, google_url, merchant_url, offer_key, unwrap_google_link,
)
from offer_matching import matches
from policy import CannotVerify, brl, parse_brl
from runtime import BotStopped, check_stop, wait

CARD_SELECTOR = "g-inner-card[data-title-id], .sh-dgr__grid-result, .sh-dlr__list-result, [data-shopping-card]"
ROW_SELECTOR = "a.P9159d, .sh-osd__offer-row, [data-shopping-offer], [data-comprahub-sponsored]"


class ShoppingAccessRequired(CannotVerify):
    pass


def search_url(query):
    return "https://www.google.com/search?" + urlencode({"q":query,"udm":"28","hl":"pt-BR","gl":"br","tbs":"p_ord:p"})


def needs_access(page,response=None):
    p=urlsplit(page.url)
    if response and response.status in (401,403,429,503):
        return True
    return (
        p.hostname in {"consent.google.com","accounts.google.com"}
        or (p.hostname in GOOGLE_HOSTS and p.path.startswith("/sorry"))
        or any(n.is_visible() for n in page.locator("#captcha-form, #recaptcha, iframe[src*='recaptcha'], input[name='captcha']").all())
    )


def amount(text):
    lines=text.replace("\xa0"," ").splitlines()
    if not lines:
        raise CannotVerify("Preço principal do Google ausente.")
    line=lines[0].strip()
    if re.search(r"/m[eê]s|\bx\s*\d+\b|^\d+\s*x",line,re.I):
        raise CannotVerify("O resultado só mostra parcela, não o preço do produto.")
    found=re.findall(r"R\$\s*((?:[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+)(?:,[0-9]{2})?)",line)
    if len(found)!=1:
        raise CannotVerify("Preço principal do Google ausente ou ambíguo.")
    value=found[0]
    cents=parse_brl("R$ "+value+(",00" if "," not in value else ""))
    if cents<=0:
        raise CannotVerify("Preço inválido.")
    return cents


CARD_SCRIPT = r"""selector => [...document.querySelectorAll(selector)]
.filter(el=>el.getClientRects().length && !el.closest('[role="dialog"]'))
.map((el,i)=>{
    const box=[...el.querySelectorAll('.UC8ZCe')].find(n=>n.innerText.includes('R$'));
    const fields=box?[...box.querySelectorAll(':scope > .mhqZ2c')]:[];
    const title=fields[0]?.innerText || el.querySelector('[data-title],h3,.tAxDx')?.textContent || '';
    const price=fields[1]?.innerText || el.querySelector('[data-price],.a8Pemb')?.textContent || '';
    const store=fields[2]?.innerText || el.querySelector('[data-merchant],.aULzUe')?.textContent || '';
    const direct=el.querySelector('a[data-merchant-url],a.shntl[href]');
    const token=el.getAttribute('data-title-id') || el.getAttribute('data-shopping-card') || [title,price,store,direct?.href || ''].join('|');
    el.setAttribute('data-comprahub-card',token);
    return {token,title:title.trim(),price:price.trim(),merchant:store.replace(/\s*e mais\s*$/i,'').trim(),
      url:direct?.href || '', text:box?.innerText || el.innerText};
})""";

ROW_SCRIPT = r"""(root, selector) => {
for(const a of root.querySelectorAll("a.RiBg6b")) {
 let row=a.parentElement;
 for(let i=0;i<5 && row;i++,row=row.parentElement) {
  if(row===root || !root.contains(row)) break;
  if(row.querySelectorAll("a.RiBg6b").length>1) break;
  if(row.querySelector("a.P9159d")) break;
  if(/R\$/.test(row.innerText||"")) row.setAttribute("data-comprahub-sponsored","");

 }
}
return [...root.querySelectorAll(selector)]
.filter(el=>el.getClientRects().length)
.filter(el=>!el.hasAttribute('data-comprahub-sponsored') || !el.parentElement.closest('[data-comprahub-sponsored]'))
.map(el=>{
    const lines=(el.innerText||'').split('\n').map(t=>t.trim()).filter(Boolean);
    const pi=lines.findIndex(t=>/^R\$\s*\d/.test(t));
    const title=el.querySelector('[data-title]')?.textContent ||
      lines.slice(pi+1).find(t=>!/^(\d+\s*x|em estoque|frete|entrega|grátis|até\b|devolu|copiar|R\$)/i.test(t)) || '';
    const store=el.querySelector('[data-merchant]')?.textContent || lines.slice(0,pi).filter(t=>!/^melhor preço$/i.test(t)).at(-1) || '';
    return {title:title.trim(),merchant:store.trim(),price:pi>=0?lines[pi]:'',
      url:el.href || el.querySelector('a[href]')?.href || '',
      text:el.innerText,shipping:lines.slice(pi+1).filter(t=>/frete|entrega|grátis/i.test(t)).join(' ').slice(0,220)};
})}""";


def resolve_link(context,link,excluded,merchant="",base="https://www.google.com"):
    """Só consulta redirecionamentos do Google; nunca carrega a loja para resolver."""
    url=unwrap_google_link(link,base)
    if excluded_store(merchant,url,excluded):
        return None
    for _ in range(5):
        check_stop()
        if not google_url(url):
            url=merchant_url(url)
            return None if excluded_store(merchant,url,excluded) else url
        response=context.request.get(url,max_redirects=0,timeout=10000)
        try:
            if response.status in (401,403,429,503):
                raise ShoppingAccessRequired("O Google bloqueou a consulta aos links das lojas; confira a verificação de acesso na janela.")
            if response.status not in (301,302,303,307,308):
                raise CannotVerify("O Google não liberou o link da loja. Confira o acesso na janela.")
            location=response.headers.get("location","")
            if not location:
                raise CannotVerify("Redirecionamento da loja sem destino.")
            target=urljoin(url,location)
            if google_url(target) and urlsplit(target).path.startswith("/sorry"):
                raise ShoppingAccessRequired("O Google pediu verificação para liberar o link da loja.")
            url=unwrap_google_link(target)
        finally:
            response.dispose()
    raise CannotVerify("O link da oferta tem redirecionamentos demais.")


def make_offer(raw,context,query,excluded,base):
    if not matches(query,raw["title"]) or excluded_store(raw["merchant"],raw.get("url",""),excluded):
        return None
    if re.search(r"sem estoque|indispon[ií]vel|esgotado",raw.get("text",""),re.I):
        return None
    cents=amount(raw["price"])
    url=resolve_link(context,raw["url"],excluded,raw["merchant"],base)
    if not url:
        return None
    host=urlsplit(url).hostname
    shop="amazon" if host in {"amazon.com.br","www.amazon.com.br"} else "external"
    key=offer_key(url)
    if shop=="amazon":
        from hub_store import normalize_url
        key,url=normalize_url(url)
    return {"key":key,"marketplace":shop,"source":"google_shopping","merchant_name":raw["merchant"],
            "title":raw["title"],"url":url,"unit_cents":cents,"shipping_cents":None,
            "shipping_note":("Preço convertido anunciado; confirme moeda, impostos e frete na loja." if re.search(r"US\$|€",raw.get("text","")) else raw.get("shipping") or "Frete e condições a confirmar na loja"),
            "coupon_available":bool(re.search(r"\bcupom\b",raw.get("text",""),re.I))}


def active_panel(page):
    for panel in page.locator('[role="dialog"]').all():
        if panel.is_visible() and any(n.is_visible() for n in panel.locator(ROW_SELECTOR+", a.RiBg6b").all()):
            return panel
    return None


def read_panel(page,context,query,excluded,info,max_expansions=30,progress=None):
    # O Google carrega o painel por rede depois de abri-lo.
    page.locator(":is("+ROW_SELECTOR+", a.RiBg6b):visible").first.wait_for(state="visible",timeout=12000)
    found={}
    previous=-1
    for _ in range(max_expansions):
        check_stop()
        panel=active_panel(page)
        if panel is None:
            raise CannotVerify("O painel de lojas do Google não foi reconhecido.")
        raw=panel.evaluate(ROW_SCRIPT,ROW_SELECTOR)
        for row in raw:
            token=(row["merchant"],row["url"],row["price"])
            if token in found:
                continue
            found[token]=None
            if excluded_store(row["merchant"],row["url"],excluded):
                info["excluded"]+=1
                continue
            if not matches(query,row["title"]):
                continue
            try:
                if progress:
                    progress(f"Google Shopping: conferindo {row['merchant']} · {row['price']}.")
                found[token]=make_offer(row,context,query,excluded,page.url)
            except ShoppingAccessRequired as error:
                info.update(status="blocked",detail=str(error))
                return [offer for offer in found.values() if offer]
            except (ValueError,Error) as error:
                found[token]=None
                info["unreadable"]+=1
                info["last_warning"]=str(error).splitlines()[0][:180]
        more=panel.get_by_role("button",name=re.compile(r"^(?:Mais lojas|Mostrar mais lojas|Ver mais lojas)$",re.I))
        buttons=[n for n in more.all() if n.is_visible() and n.is_enabled()]
        if not buttons:
            return [offer for offer in found.values() if offer]
        if len(raw)==previous:
            info["has_more"]=True
            return [offer for offer in found.values() if offer]
        previous=len(raw)
        buttons[0].press("Enter",timeout=4000)
        wait(page,1000)
    info["has_more"]=True
    return [offer for offer in found.values() if offer]


def close_panel(page):
    for button in page.get_by_role("button",name=re.compile(r"^(Fechar|Close)$",re.I)).all():
        if button.is_visible():
            button.press("Enter",timeout=4000)
            return


def collect(page,query,max_pages=30,excluded=(),progress=None):
    """Esgota os resultados carregáveis até o limite; nunca promete todo o mercado."""
    info={"status":"ok","pages":0,"results":0,"compatible":0,"priced":0,"unreadable":0,
          "excluded":0,"products_opened":0,"has_more":False,"detail":""}
    offers={}
    seen=set()
    stagnant=0
    try:
        response=page.goto(search_url(query),wait_until="domcontentloaded",timeout=30000)
        wait(page,1200)
        if needs_access(page,response):
            info.update(status="blocked",detail="O Google pediu verificação de acesso. Conclua-a na janela; o bot não resolve CAPTCHA.")
            return SearchResult([],{"google_shopping":info})
        for round_number in range(max_pages):
            check_stop()
            if progress:
                progress(f"Google Shopping: rodada {round_number+1}/{max_pages}; {len(offers)} ofertas válidas, {info['excluded']} excluídas.")
            info["pages"]+=1
            raw=page.evaluate(CARD_SCRIPT,CARD_SELECTOR)
            if not raw and round_number==0:
                body=page.locator("body").inner_text().lower()
                empty=any(t in body for t in ("nenhum resultado","não encontrou","não há resultados"))
                info.update(status="empty" if empty else "unsupported",
                            detail="Sem resultados." if empty else "O layout do Google Shopping não foi reconhecido.")
                break
            fresh=[card for card in raw if card["token"] not in seen]
            stagnant=0 if fresh else stagnant+1
            for card in fresh:
                check_stop()
                seen.add(card["token"])
                info["results"]+=1
                if not matches(query,card["title"]):
                    continue
                info["compatible"]+=1
                if card["url"]:
                    try:
                        offer=make_offer(card,page.context,query,excluded,page.url)
                        if offer: offers[(offer["merchant_name"],offer["url"])]=offer
                    except (ValueError,Error) as error:
                        info["unreadable"]+=1
                    continue
                # Mesmo um cartão cuja loja principal foi excluída pode agrupar outras lojas.
                node=next((n for n in page.locator(CARD_SELECTOR).all()
                           if n.get_attribute("data-comprahub-card")==card["token"]),None)
                if node is None:
                    info["unreadable"]+=1
                    continue
                try:
                    opener=node.locator('[role="button"][aria-label*="visualização de produtos"]')
                    if opener.count():
                        opener.first.press("Enter",timeout=4000)
                    else:
                        node.click(timeout=4000)
                    info["products_opened"]+=1
                    if progress:
                        progress(f"Google Shopping: lojas do produto {info['products_opened']} · {card['title'][:100]}.")
                    for offer in read_panel(page,page.context,query,excluded,info,progress=progress):
                        offers[(offer["merchant_name"],offer["url"])]=offer
                except (ValueError,Error) as error:
                    info["unreadable"]+=1
                    info["last_warning"]=str(error).splitlines()[0][:180]
                    if needs_access(page):
                        info.update(status="blocked",detail="O Google pediu verificação durante a varredura.")
                        break
                finally:
                    close_panel(page)
                if info["status"]=="blocked":
                    break
            if info["status"]=="blocked":
                break
            next_link=page.locator('a#pnnext, a[aria-label="Próxima"], a[aria-label="Próxima página"]')
            next_visible=[n for n in next_link.all() if n.is_visible()]
            if next_visible:
                next_url=urljoin(page.url,next_visible[0].get_attribute("href") or "")
                if not google_url(next_url):
                    raise CannotVerify("A paginação saiu do Google Shopping.")
                response=page.goto(next_url,wait_until="domcontentloaded",timeout=30000)
                wait(page,1200)
                if needs_access(page,response):
                    info.update(status="blocked",detail="O Google pediu verificação durante a paginação.")
                    break
            else:
                more=[n for n in page.get_by_role("button",name=re.compile(r"^(Mais resultados|Mostrar mais resultados)$",re.I)).all() if n.is_visible()]
                if more:
                    more[0].press("Enter",timeout=4000)
                else:
                    page.evaluate("window.scrollTo(0,document.documentElement.scrollHeight)")
                wait(page,1200)
                if stagnant>=2:
                    break
        else:
            info["has_more"]=True
    except BotStopped:
        raise
    except (ValueError,Error) as error:
        info.update(status="error",detail=str(error).splitlines()[0][:200])
    info["priced"]=len(offers)
    if info["status"]=="ok" and (info["unreadable"] or info["has_more"]):
        info["status"]="incomplete"
    if info["status"] in ("ok","incomplete"):
        info["detail"]=(
            f"{len(seen)} cartões; {info['products_opened']} produtos expandidos; {len(offers)} ofertas; "
            f"{info['excluded']} lojas excluídas; {info['unreadable']} leituras incompletas. "
            +("Há resultados não consultados." if info["has_more"] else "Fim dos resultados carregáveis nesta sessão.")
            +" Não representa todo o mercado."
        )
    return SearchResult(sorted(offers.values(),key=lambda o:(o["unit_cents"],o["merchant_name"],o["key"])),{"google_shopping":info})
