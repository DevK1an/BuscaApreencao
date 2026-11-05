"""Diagnóstico distribuído: testa componentes usando apenas páginas locais simuladas."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from urllib.request import Request, urlopen

from app_paths import RESOURCE_ROOT, VERSION, browser_environment, is_frozen


def run_checks():
    from playwright.sync_api import sync_playwright
    from amazon import add_to_cart, checkout_review, open_checkout, product_price, require_exact_cart, set_quantity, wait_for_cart_add
    from hub import HubServer, JobManager
    from hub_store import ProductStore
    from policy import Settings, check_review

    browser_environment()
    checks = {"version": VERSION, "frozen": is_frozen(), "checks": {}}
    with tempfile.TemporaryDirectory(prefix="CompraHub-check-") as folder:
        root = Path(folder)
        store = ProductStore(root / "products.json")
        item = store.save({
            "name":"Produto simulado", "url":"https://www.amazon.com.br/dp/B0778W8ZV8",
            "quantity":2, "max_price":"18,00", "interval_seconds":60,
        })
        checks["checks"]["persistent_store"] = ProductStore(root / "products.json").get(item["id"])["quantity"] == 2
        from market_search import SearchResult, select_lowest
        from offer_matching import matches
        candidate = {"key":"MLB123456","marketplace":"mercadolivre","unit_cents":1500,"title":"Lápis BIC Evolution 12 cores"}
        result = SearchResult([candidate],{"mercadolivre":{"status":"ok"}})
        checks["checks"]["price_selection"] = select_lowest(result) == candidate and matches("Lápis BIC Evolution 12 cores",candidate["title"])

        # O sinal de parada existe antes de o processo filho executar qualquer navegador.
        def stopped_worker(command, **kwargs):
            Path(kwargs["env"]["AMAZON_BOT_STOP_FILE"]).touch()
            return subprocess.Popen(command, **kwargs)

        manager = JobManager(store, root / "jobs", popen=stopped_worker)
        server = HubServer(("127.0.0.1", 0), manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for asset in ("/", "/styles.css", "/app.js", "/tracking.js"):
                with urlopen(server.origin + asset, timeout=5) as response:
                    if response.status != 200 or len(response.read()) < 100:
                        raise RuntimeError(f"Recurso indisponível: {asset}")
            checks["checks"]["hub_assets"] = True
            request = Request(server.origin + "/api/state", headers={"X-Hub-Token":server.token})
            with urlopen(request, timeout=5) as response:
                state = json.load(response)
            checks["checks"]["hub_api"] = len(state["products"]) == 1

            manager.start(item["id"])
            child = manager.jobs[item["id"]]["process"]
            code = child.wait(timeout=45)
            log = manager.logs(item["id"])
            if code != 0 or "Monitoramento parado" not in log:
                raise RuntimeError(f"Falha no processo interno: código {code}; {log[-1000:]}")
            checks["checks"]["worker_dispatch"] = True
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            manager.shutdown()

        product = """<h1 id="productTitle">Produto simulado</h1><input id="ASIN" value="B0778W8ZV8">
<div id="corePriceDisplay_desktop_feature_div"><span class="a-price priceToPay">
<span class="a-offscreen"> </span><span aria-hidden="true"><span class="a-price-symbol">R$</span>
<span class="a-price-whole">16<span class="a-price-decimal">,</span></span>
<span class="a-price-fraction">92</span></span></span></div>
<select id="quantity"><option>1</option><option>2</option></select>
<input type="button" id="add-to-cart-button" onclick="window.added=true;document.getElementById('attach-desktop-sideSheet').hidden=false">"""
        product += """<div id="promoPriceBlockMessage_feature_div">
<label><input id="coupon-simulated" type="checkbox">Aplicar cupom de 10%</label></div>"""
        product += """<div id="attach-desktop-sideSheet" role="dialog" hidden>
<div id="attach-warranty-pane"><h2>Adicionar ao seu pedido</h2><p>Garantia Estendida: cobrir este produto</p>
<span id="attachSiNoCoverage" onclick="window.declined=true;document.getElementById('attach-desktop-sideSheet').hidden=true;setTimeout(()=>fetch('/simulated-add',{method:'POST'}),200)"><span>Não, obrigado</span></span>
<button id="attachSiAddCoverage" onclick="window.insured=true">Incluir proteção</button>
</div></div>"""
        google_page = "<div data-shopping-card=\"check\"><span data-title>Lápis BIC Evolution 12 cores</span><span data-price>R$ 16,92</span><span data-merchant>Amazon</span><div role=\"button\" tabindex=\"0\" aria-label=\"visualização de produtos\" onkeydown=\"if(event.key==='Enter')document.querySelector('#google-panel').hidden=false\"></div></div><div id=\"google-panel\" role=\"dialog\" hidden><a class=\"P9159d\" href=\"https://shopee.com.br/cheap\">Shopee<br>R$ 1,00<br>Lápis BIC Evolution 12 cores</a><a class=\"P9159d\" href=\"https://www.amazon.com.br/dp/B0778W8ZV8\">Amazon<br>R$ 16,92<br>Lápis BIC Evolution 12 cores</a><a class=\"P9159d\" href=\"https://lojaazul.com.br/item\">Loja Azul<br>R$ 15,99<br>Lápis BIC Evolution 12 cores</a><button onclick=\"this.parentElement.hidden=true\">Fechar</button></div>"
        cart = """<div id="sc-active-cart"><div data-asin="B0778W8ZV8" data-itemtype="active" data-quantity="2">Produto</div></div>
<input type="button" name="proceedToRetailCheckout" onclick="location.href='/checkout'">"""
        review = """<div id="spc-orders"><a href="/dp/B0778W8ZV8">Produto</a><span>Quantidade: 2</span></div>
<table id="subtotals-marketplace-table"><tr><td>Total do pedido:</td><td>R$ 34,84</td></tr></table>
<input name="placeYourOrder1" type="button" onclick="window.purchased=true">"""
        with sync_playwright() as pw:
            # Usa o Chromium completo incluído no .exe, sem depender do headless-shell.
            context = pw.chromium.launch_persistent_context(
                str(root / "browser-profile"), headless=True,
                executable_path=pw.chromium.executable_path, locale="pt-BR",
            )
            page = context.pages[0] if context.pages else context.new_page()
            cart_state = {"added":False}
            def simulated(route):
                path = route.request.url
                if path.endswith("/simulated-add"):
                    cart_state["added"] = True
                    route.fulfill(content_type="application/json",body='{"ok":true}')
                    return
                if "google.com/search" in path:
                    route.fulfill(content_type="text/html; charset=utf-8",body=google_page)
                    return
                active_cart = cart if cart_state["added"] else '<div id="sc-active-cart">Seu carrinho está vazio</div>'
                html = active_cart if "/gp/cart/" in path else review if "/checkout" in path else product
                route.fulfill(content_type="text/html; charset=utf-8", body=html)
            context.route("**/*", simulated)
            try:
                import google_shopping as shopping
                old_wait=shopping.wait
                shopping.wait=lambda page,ms:page.wait_for_timeout(min(ms,20))
                try:
                    found=shopping.collect(page,"Lápis BIC Evolution 12 cores",3,("Shopee","AliExpress"))
                finally:
                    shopping.wait=old_wait
                checks["checks"]["google_shopping"] = [o["unit_cents"] for o in found.offers] == [1599,1692]
                checks["checks"]["store_exclusions"] = not any("shopee" in o["url"] for o in found.offers)
                page.goto("https://www.amazon.com.br/dp/B0778W8ZV8")
                if product_price(page, "B0778W8ZV8") != 1692:
                    raise RuntimeError("Leitura do preço simulado falhou.")
                from coupons import CouponApplier
                coupon = CouponApplier("amazon")
                if not coupon.apply(page) or coupon.apply(page) or not page.locator("#coupon-simulated").is_checked():
                    raise RuntimeError("A ativação única do cupom simulado falhou.")
                checks["checks"]["automatic_coupon"] = True
                set_quantity(page, 2)
                add_to_cart(page)
                if not page.evaluate("window.added"):
                    raise RuntimeError("Preparação do carrinho simulado falhou.")
                wait_for_cart_add(page,"B0778W8ZV8",2)
                checks["checks"]["async_cart_verification"] = True
                if not page.evaluate("window.declined") or page.evaluate("window.insured"):
                    raise RuntimeError("A recusa do seguro simulado falhou.")
                checks["checks"]["insurance_declined"] = True
                page.goto("https://www.amazon.com.br/gp/cart/view.html")
                require_exact_cart(page, "B0778W8ZV8", 2)
                open_checkout(page)
                check_review(checkout_review(page), Settings("B0778W8ZV8", 1800, 60), 2)
                if page.evaluate("window.purchased"):
                    raise RuntimeError("A confirmação final foi acionada indevidamente.")
                checks["checks"]["bundled_browser"] = True
                checks["checks"]["simulated_checkout"] = True
                checks["checks"]["manual_confirmation_preserved"] = True
            finally:
                context.close()
    if not all(checks["checks"].values()):
        raise RuntimeError("Um dos componentes não passou no diagnóstico.")
    checks["ok"] = True
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Diagnóstico offline do CompraHub, sem compras.")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_checks()
        code = 0
    except Exception as error:
        import traceback
        result = {"ok":False, "error":str(error), "traceback":traceback.format_exc()}
        code = 1
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text, flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    return code
