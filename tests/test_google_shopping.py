"""Google Shopping: exclusões, preços e abertura sem compras fora da Amazon."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from urllib.parse import quote

from playwright.sync_api import sync_playwright
from app_paths import browser_environment
from google_shopping import collect,amount,make_offer,resolve_link
from merchant_rules import merchant_url,normalize_exclusions,excluded_store,unwrap_google_link
from hub_store import ProductStore,validate_product
from price_tracker import SearchSettings
from policy import CannotVerify
from market_search import SearchResult
from test_tracking import QUERY,offer
from test_price_reading import html as amazon_html

browser_environment()


def shopping_data(**changes):
    return {"mode":"search","search_provider":"google_shopping","query":QUERY,
            "quantity":1,"max_price":"20,00","pages_per_store":3,**changes}


def row(name,price,url,title=QUERY):
    return f'<a class="P9159d" href="{url}">{name}<br>R$ {price}<br>{title}<br>Frete grátis</a>'


def card(token="a",title=QUERY):
    return f'''<div data-shopping-card="{token}"><span data-title>{title}</span><span data-price>R$ 18,00</span><span data-merchant>Shopee</span>
<div role="button" tabindex="0" aria-label="visualização de produtos" onkeydown="if(event.key==='Enter')document.querySelector('#panel').hidden=false"></div></div>'''


def panel():
    return '<div id="panel" role="dialog" hidden>'+row("Shopee","1,00","https://shopee.com.br/cheap")+row("Amazon.com.br","18,00","https://www.amazon.com.br/dp/B0778W8ZV8")+row("Loja Azul","17,00","https://lojaazul.com.br/produto")+'''<button onclick="document.querySelector('#extra').hidden=false;this.hidden=true">Mais lojas</button><div id="extra" hidden>'''+row("AliExpress","0,50","https://aliexpress.com/cheap")+row("Loja Verde","16,00","https://lojaverde.com.br/produto")+'''</div><a href="https://related.example/item">Produto relacionado R$ 0,01</a><button onclick="this.parentElement.hidden=true">Fechar</button></div>'''


class RulesTests(unittest.TestCase):
    def test_store_names_domains_and_aliases_are_blocked(self):
        for excluded in (["AliExpress","Shopee"],["aliexpress.com","shopee.com.br"]):
            for name,url in (("Ali Express","https://www.google.com/goto?url=opaque"),
                             ("Shopee Brasil","https://www.google.com/goto?url=opaque"),
                             ("Loja","https://br.aliexpress.com/item/123"),
                             ("Outra","https://www.shopee.com.br/item")):
                self.assertTrue(excluded_store(name,url,normalize_exclusions(excluded)))
        self.assertFalse(excluded_store("Loja Azul","https://lojaazul.com.br/x",["Shopee"]))

    def test_custom_domains_match_subdomains_but_not_deceptive_suffixes(self):
        rules=normalize_exclusions(["https://www.lojaz.com.br/path","Loja Azul","Loja Azul"])
        self.assertEqual(len(rules),2)
        self.assertTrue(excluded_store("Z","https://ofertas.lojaz.com.br/a",rules))
        self.assertFalse(excluded_store("Z","https://lojaz.com.br.evil.example/a",["lojaz.com.br"]))
        self.assertTrue(excluded_store("Loja Azul - Oficial","https://public.example/x",rules))

    def test_unsafe_destinations_and_invalid_exclusion_lists_fail(self):
        for url in ("http://loja.com/p","https://localhost/p","https://127.0.0.1/p","https://[::1]/",
                    "https://x.local/p","file:///tmp/x","javascript:alert(1)","https://user:pass@loja.com/p"):
            with self.subTest(url=url),self.assertRaises(ValueError):
                merchant_url(url)
        for value in ("Shopee",[None],["*"],["a"],["bad\nstore"]):
            with self.subTest(value=value),self.assertRaises(ValueError):
                normalize_exclusions(value)
        self.assertEqual(normalize_exclusions([]),())

    def test_current_and_rounded_prices_ignore_old_price_and_installments(self):
        self.assertEqual(amount("R$ 3.599,90\nPreço normal: R$ 3.799"),359990)
        self.assertEqual(amount("R$ 3.799"),379900)
        for value in ("10x de R$ 100,00","R$ 100,00/mês","R$ 1,00 R$ 2,00","US$ 10",""):
            with self.subTest(value=value),self.assertRaises(CannotVerify):
                amount(value)

    def test_redirect_resolution_never_fetches_excluded_merchant(self):
        response=Mock(status=302,headers={"location":"https://shopee.com.br/item"})
        context=SimpleNamespace(request=Mock())
        context.request.get.return_value=response
        self.assertIsNone(resolve_link(context,"https://www.google.com/goto?url=opaque",["Shopee"],"Loja"))
        context.request.get.assert_called_once_with("https://www.google.com/goto?url=opaque",max_redirects=0,timeout=10000)
        response.dispose.assert_called_once()

    def test_name_exclusion_happens_before_any_redirect_request(self):
        context=SimpleNamespace(request=Mock())
        self.assertIsNone(resolve_link(context,"https://www.google.com/goto?url=opaque",["Shopee"],"Shopee"))
        context.request.get.assert_not_called()

    def test_plain_and_encrypted_google_links_are_supported(self):
        direct="https://www.amazon.com.br/dp/B0778W8ZV8"
        self.assertEqual(unwrap_google_link("https://www.google.com/url?q="+quote(direct,safe="")),direct)
        response=Mock(status=302,headers={"location":direct})
        context=SimpleNamespace(request=Mock())
        context.request.get.return_value=response
        self.assertEqual(resolve_link(context,"/goto?url=opaque",[],"Amazon"),direct)

    def test_google_config_defaults_and_roundtrips_exclusions(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ProductStore(Path(folder)/"products.json")
            saved=store.save(shopping_data())
            self.assertEqual(saved["excluded_stores"],["AliExpress","Shopee"])
            self.assertEqual(saved["marketplaces"],["google_shopping"])
            self.assertIn("udm=28",saved["url"])
            self.assertEqual(saved,ProductStore(store.path).get(saved["id"]))
            custom=store.save(shopping_data(excluded_stores=["kabum.com.br"]),saved["id"])
            self.assertEqual(custom["excluded_stores"],["kabum.com.br"])

    def test_legacy_saved_searches_migrate_without_changing_budget_quantity_or_id(self):
        from hub_store import validate_product
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"products.json"
            saved={**validate_product({"mode":"search","query":QUERY,"quantity":2,"max_price":"18,00"}),"id":"a"*32}
            saved.pop("search_provider",None)
            path.write_text(json.dumps([saved]),encoding="utf-8")
            item=ProductStore(path).list()[0]
            self.assertEqual((item["id"],item["quantity"],item["max_unit_cents"]),("a"*32,2,1800))
            self.assertEqual(item["search_provider"],"google_shopping")
            self.assertEqual(item["pages_per_store"],30)

    def test_other_store_direct_links_are_open_only_products(self):
        product=validate_product({"url":"https://lojaazul.com.br/produto","quantity":2,"max_price":"20"})
        self.assertEqual(product["marketplace"],"external")
        with self.assertRaises(ValueError):
            validate_product({"url":"https://www.amazon.com.br/s?k=lapis","quantity":1,"max_price":"20"})


class ShoppingBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context()
        self.page=self.context.new_page()
        self.visits=[]
        self.body=card()+panel()
        def serve(route):
            self.visits.append(route.request.url)
            route.fulfill(content_type="text/html; charset=utf-8",body=self.body)
        self.context.route("**/*",serve)
    def tearDown(self):
        self.context.close()
    def collect(self,**kwargs):
        with patch("google_shopping.wait",side_effect=lambda page,ms:page.wait_for_timeout(10)):
            return collect(self.page,QUERY,excluded=("Shopee","AliExpress"),**kwargs)

    def test_grouped_card_expands_other_stores_and_excludes_cheaper_blocked_shops(self):
        result=self.collect(max_pages=4)
        self.assertEqual([o["unit_cents"] for o in result.offers],[1600,1700,1800])
        self.assertEqual(result.offers[0]["merchant_name"],"Loja Verde")
        self.assertEqual(result.coverage["google_shopping"]["status"],"ok")
        self.assertEqual(result.coverage["google_shopping"]["excluded"],2)
        self.assertTrue(all("google.com" in url for url in self.visits))
        self.assertEqual(result.offers[-1]["marketplace"],"amazon")
        self.assertIsNone(result.offers[0]["shipping_cents"])

    def test_challenge_returns_blocked_coverage_without_fabricated_prices(self):
        self.body='<form id="captcha-form">Verificação de acesso</form>'
        result=self.collect(max_pages=3)
        self.assertEqual(result.coverage["google_shopping"]["status"],"blocked")
        self.assertEqual(result.offers,[])
        self.assertEqual(len(self.visits),1)

    def test_round_limit_reports_partial_coverage(self):
        result=self.collect(max_pages=1)
        self.assertTrue(result.coverage["google_shopping"]["has_more"])
        self.assertEqual(result.coverage["google_shopping"]["status"],"incomplete")
        self.assertIn("Não representa todo o mercado",result.coverage["google_shopping"]["detail"])

    def test_unknown_layout_does_not_select_arbitrary_search_links(self):
        self.body='<a href="https://lojaazul.com.br/a">Lápis por R$ 1,00</a>'
        result=self.collect(max_pages=2)
        self.assertEqual(result.offers,[])
        self.assertEqual(result.coverage["google_shopping"]["status"],"unsupported")

    def test_sponsored_offer_rows_are_read_but_related_products_are_not(self):
        self.body=card()+panel().replace('<a class="P9159d"', '<div><a class="RiBg6b" href="https://lojaroxa.com.br/item">Loja Roxa</a><div>R$ 15,00</div><div>'+QUERY+'</div></div><a class="P9159d"',1)
        result=self.collect(max_pages=4)
        self.assertEqual(result.offers[0]["merchant_name"],"Loja Roxa")
        self.assertEqual(result.offers[0]["unit_cents"],1500)
        self.assertFalse(any(o["unit_cents"]==1 for o in result.offers))

    def test_external_selection_never_prepares_any_store_cart(self):
        from shopping_runner import dispatch_candidates
        result=SearchResult([{**offer(price=1800),"marketplace":"amazon","merchant_name":"Amazon"},
                             {**offer(price=1600),"marketplace":"external","merchant_name":"Loja Azul","url":"https://lojaazul.com.br/item"}],
                            {"google_shopping":{"status":"ok"}})
        settings=SearchSettings.load({**shopping_data(),"max_unit_cents":2000})
        def opened(page,offer,quantity):
            self.assertEqual(page.url,"https://lojaazul.com.br/item")
            self.assertEqual(quantity,2)
            self.assertIsNone(page.evaluate("window.purchased"))
        with patch("shopping_runner.open_store_product",side_effect=opened) as open_product,patch("price_tracker.prepare_amazon") as amazon,patch("mercadolivre.prepare") as ml:
            self.assertTrue(dispatch_candidates(self.page,self.context,settings,2,result))
        open_product.assert_called_once()
        amazon.assert_not_called()
        ml.assert_not_called()

    def test_amazon_selection_preserves_preparation_and_original_budget(self):
        from shopping_runner import dispatch_candidates
        candidate={**offer(price=1692),"merchant_name":"Amazon"}
        result=SearchResult([candidate],{"google_shopping":{"status":"ok"}})
        self.body=amazon_html().replace('<h1 id="productTitle">Lápis</h1>',f'<h1 id="productTitle">{QUERY}</h1>')
        settings=SearchSettings.load({**shopping_data(),"max_unit_cents":1800})
        with patch("price_tracker.prepare_amazon") as prepare:
            self.assertTrue(dispatch_candidates(self.page,self.context,settings,2,result))
            self.assertEqual(prepare.call_args.args[2].max_unit_cents,1800)
            self.assertEqual(prepare.call_args.args[3],2)

    def test_excluded_amazon_is_not_opened_or_prepared(self):
        from shopping_runner import dispatch_candidates
        result=SearchResult([{**offer(),"merchant_name":"Amazon.com.br"}],{"google_shopping":{"status":"ok"}})
        settings=SearchSettings.load({**shopping_data(excluded_stores=["Amazon"]),"max_unit_cents":2000})
        with patch("price_tracker.prepare_amazon") as prepare:
            self.assertFalse(dispatch_candidates(self.page,self.context,settings,1,result))
            prepare.assert_not_called()
        self.assertEqual(self.visits,[])

    def test_redirect_to_excluded_store_is_blocked_before_navigation(self):
        from shopping_runner import install_navigation_filter
        self.context.unroute("**/*")
        self.context.route("**/*",lambda route:route.fulfill(status=302,headers={"location":"https://shopee.com.br/item"}) if "lojaazul" in route.request.url else route.fulfill(body="blocked should not load"))
        install_navigation_filter(self.page,["Shopee"])
        with self.assertRaises(Exception):
            self.page.goto("https://lojaazul.com.br/item")

    def test_external_direct_mode_never_clicks_purchase_buttons(self):
        from shopping_runner import run_external_link
        self.body='<button onclick="window.purchased=true">Comprar agora</button>'
        def close(page,ms):
            self.assertIsNone(page.evaluate("window.purchased"))
            page.close()
        with patch("shopping_runner.sync_playwright") as factory,patch("shopping_runner.wait",side_effect=close):
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value=self.context
            run_external_link({"url":"https://lojaazul.com.br/item","max_unit_cents":2000},2)

    def test_pagination_reads_new_cards_and_keeps_google_scope(self):
        self.context.unroute("**/*")
        def serve(route):
            second="start=40" in route.request.url
            key="b" if second else "a"
            direct=f'<div data-shopping-card="{key}"><span data-title>{QUERY}</span><span data-price>R$ {"15,00" if second else "18,00"}</span><span data-merchant>Loja Azul</span><a data-merchant-url href="https://lojaazul.com.br/{key}">Oferta</a></div>'
            route.fulfill(content_type="text/html; charset=utf-8",body=direct+('' if second else '<a id="pnnext" href="/search?udm=28&start=40">Próxima</a>'))
        self.context.route("**/*",serve)
        result=self.collect(max_pages=5)
        self.assertEqual([o["unit_cents"] for o in result.offers],[1500,1800])
        self.assertEqual(result.coverage["google_shopping"]["results"],2)

    def test_foreign_pagination_does_not_leave_google(self):
        self.body=card()+panel()+'<a id="pnnext" href="https://foreign.example/a">Próxima</a>'
        result=self.collect(max_pages=4)
        self.assertEqual(result.coverage["google_shopping"]["status"],"error")
        self.assertFalse(any("foreign.example" in url for url in self.visits))

    def test_scroll_loads_additional_products(self):
        direct=lambda token,price:f'<div data-shopping-card="{token}"><span data-title>{QUERY}</span><span data-price>R$ {price}</span><span data-merchant>Loja Azul</span><a data-merchant-url href="https://lojaazul.com.br/{token}">Oferta</a></div>'
        second=direct("b","15,00")
        self.body=direct("a","18,00")+'<script>window.scrollTo=()=>{if(!window.loaded){window.loaded=true;document.body.insertAdjacentHTML("beforeend",'+json.dumps(second)+');}};</script>'
        result=self.collect(max_pages=5)
        self.assertEqual([o["unit_cents"] for o in result.offers],[1500,1800])

    def test_price_update_in_amazon_reorders_before_preparation(self):
        from shopping_runner import dispatch_candidates
        self.body=amazon_html().replace('<h1 id="productTitle">Lápis</h1>',f'<h1 id="productTitle">{QUERY}</h1>')
        amazon={**offer(price=1500),"merchant_name":"Amazon"}
        other={**offer(price=1600),"marketplace":"external","merchant_name":"Loja Azul","url":"https://lojaazul.com.br/item"}
        settings=SearchSettings.load({**shopping_data(),"max_unit_cents":2000})
        with patch("shopping_runner.open_store_product") as opened,patch("price_tracker.prepare_amazon") as prepare:
            self.assertTrue(dispatch_candidates(self.page,self.context,settings,1,SearchResult([amazon,other],{"google_shopping":{"status":"ok"}})))
        prepare.assert_not_called()
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[1]["merchant_name"],"Loja Azul")


    def test_blocked_offer_lookup_stops_without_repeated_requests(self):
        from google_shopping import ShoppingAccessRequired
        self.page.goto("https://www.google.com/search?udm=28")
        self.page.locator("#panel").evaluate("el=>el.hidden=false")
        from google_shopping import read_panel
        info={"unreadable":0,"excluded":0,"has_more":False,"status":"ok"}
        with patch("google_shopping.resolve_link",side_effect=ShoppingAccessRequired("Verificação Google")) as resolve:
            offers=read_panel(self.page,self.context,QUERY,("Shopee","AliExpress"),info)
        self.assertEqual(offers,[])
        self.assertEqual(info["status"],"blocked")
        resolve.assert_called_once()

    def test_progress_reports_product_and_store_being_checked(self):
        messages=[]
        with patch("google_shopping.wait",side_effect=lambda page,ms:page.wait_for_timeout(10)):
            collect(self.page,QUERY,3,("Shopee","AliExpress"),messages.append)
        self.assertTrue(any("lojas do produto" in message for message in messages))
        self.assertTrue(any("conferindo Loja" in message for message in messages))
