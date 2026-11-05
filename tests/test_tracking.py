import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from app_paths import browser_environment
from hub_store import ProductStore, normalize_url, validate_product
from market_search import SearchResult, collect, parse_amount, read_cards, select_lowest
from offer_matching import matches
from policy import CannotVerify, check_review
from price_history import PriceHistory
from price_tracker import SearchSettings

browser_environment()
QUERY = "Lápis de cor BIC Evolution 12 cores"


def offer(store="amazon", price=1692, key="B0778W8ZV8"):
    return {"marketplace":store,"key":key,"unit_cents":price,"title":QUERY,
            "url":"https://www.amazon.com.br/dp/B0778W8ZV8" if store=="amazon" else "https://www.mercadolivre.com.br/lapis/p/MLB123456",
            "shipping_cents":None,"shipping_note":"Frete a confirmar"}


def amazon_card(price=1692):
    return f"""<div data-component-type="s-search-result" data-asin="B0778W8ZV8">
<a href="/dp/B0778W8ZV8"><h2>{QUERY}</h2></a>
<span class="a-price"><span class="a-offscreen">R$ {price//100},{price%100:02d}</span>
<span class="a-price-symbol">R$</span><span class="a-price-whole">{price//100},</span><span class="a-price-fraction">{price%100:02d}</span></span>
<span class="a-price a-text-price"><span class="a-offscreen">R$ 1,00</span></span>
<div data-cy="delivery-recipe">Frete grátis acima de R$ 79,00</div></div>"""


def ml_card(price=1590):
    return f"""<div class="ui-search-result__wrapper"><div class="poly-card">
<a class="poly-component__title" href="https://www.mercadolivre.com.br/lapis/p/MLB123456">{QUERY}</a>
<div class="poly-price__current"><span class="andes-money-amount"><span class="andes-money-amount__currency-symbol">R$</span>
<span class="andes-money-amount__fraction">{price//100}</span><span class="andes-money-amount__cents">{price%100:02d}</span></span></div>
<div class="poly-component__shipping">Frete grátis</div></div></div>"""


class MatchingTests(unittest.TestCase):
    def test_brand_model_and_numbers_must_match(self):
        self.assertTrue(matches(QUERY, QUERY + ", 1 unidade"))
        self.assertFalse(matches(QUERY, "Lápis de cor Faber Castell 12 cores"))
        self.assertFalse(matches("Nintendo Switch 2", "Nintendo Switch OLED 64GB"))
        self.assertFalse(matches("Nintendo Switch 2", "Capa para Nintendo Switch 2"))
        self.assertFalse(matches("Nintendo Switch 2", "Nintendo Switch 2 usado"))
        self.assertFalse(matches("Nintendo Switch 2", "Kit Nintendo Switch 2"))
        self.assertTrue(matches("Capa Nintendo Switch 2", "Capa Nintendo Switch 2 original"))

    def test_name_search_validates_and_roundtrips_without_a_product_link(self):
        data = {"mode":"search","query":QUERY,"quantity":2,"max_price":"18,00","marketplaces":["amazon","mercadolivre"],"pages_per_store":3}
        with tempfile.TemporaryDirectory() as folder:
            store = ProductStore(Path(folder)/"products.json")
            saved = store.save(data)
            loaded = ProductStore(store.path).get(saved["id"])
            self.assertEqual(saved,loaded)
            self.assertEqual(saved["max_unit_cents"],1800)
            self.assertEqual(saved["mode"],"search")

    def test_invalid_searches_are_rejected(self):
        base={"mode":"search","query":QUERY,"quantity":1,"max_price":"18"}
        for changes in (
            {"query":None},{"query":"lapis"},{"query":"lapis original"},{"query":"x\nmodelo"},
            {"marketplaces":[]},{"marketplaces":[{}]},{"marketplaces":["evil"]},
            {"pages_per_store":100},{"interval_seconds":1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_product({**base,**changes})

    def test_marketplace_links_keep_product_id_and_discard_tracking(self):
        key,url=normalize_url("https://produto.mercadolivre.com.br/MLB-1234567-lapis-_JM?tracking=abc")
        self.assertEqual(key,"MLB1234567")
        self.assertNotIn("tracking",url)
        key,url=normalize_url("https://www.mercadolivre.com.br/lapis/p/MLB123456?pdp_filters=item_id%3AMLB987654")
        self.assertEqual(key,"MLB987654")
        self.assertIn("pdp_filters=",url)
        with self.assertRaises(ValueError):
            normalize_url("https://mercadolivre.com.br.evil.example/p/MLB123456")

    def test_lowest_selection_never_claims_unknown_shipping_is_zero(self):
        a,b=offer(price=1800),offer("mercadolivre",1500,"MLB123456")
        result=SearchResult([a,b],{"amazon":{"status":"ok"},"mercadolivre":{"status":"ok"}})
        self.assertEqual(select_lowest(result),b)
        self.assertIsNone(b["shipping_cents"])
        result.coverage["mercadolivre"]["status"]="blocked"
        self.assertIsNone(select_lowest(result))

    def test_marketplace_integer_prices_have_zero_cents_but_incomplete_amounts_fail(self):
        self.assertEqual(parse_amount({"symbol":"R$","whole":"1.234"},"mercadolivre"),123400)
        with self.assertRaises(CannotVerify):
            parse_amount({"symbol":"R$","whole":"1.234"},"amazon")
        with self.assertRaises(CannotVerify):
            parse_amount({"text":"R$ 1,00","symbol":"R$","whole":"2","fraction":"00"},"amazon")


class HistoryTests(unittest.TestCase):
    def test_prices_are_durable_and_isolated_per_product(self):
        with tempfile.TemporaryDirectory() as folder:
            history=PriceHistory(Path(folder)/"prices.sqlite3")
            history.record("first",[offer(price=1800)],{"amazon":{"status":"ok"}})
            history.record("first",[offer(price=1692)],{"amazon":{"status":"ok"}})
            history.record("other",[offer(price=100)],{"amazon":{"status":"ok"}})
            data=PriceHistory(history.path).get("first")
            self.assertEqual([p["min_unit_cents"] for p in data["series"]],[1800,1692])
            self.assertEqual(data["latest"]["offers"][0]["unit_cents"],1692)
            self.assertIsNone(data["latest"]["offers"][0]["shipping_cents"])
            self.assertEqual(history.get("' OR 1=1--")["series"],[])

    def test_blocked_cycle_does_not_fabricate_prices_or_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            history=PriceHistory(Path(folder)/"prices.sqlite3")
            history.record("first",[],{"mercadolivre":{"status":"blocked"}},note="HTTP 403")
            data=history.get("first")
            self.assertIsNone(data["series"][0]["min_unit_cents"])
            self.assertFalse(data["series"][0]["complete"])
            self.assertIsNone(data["latest"]["selected"])


class SearchBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context()
        self.page=self.context.new_page()
    def tearDown(self):
        self.context.close()
    def serve(self,html,url):
        self.context.route("**/*",lambda route:route.fulfill(content_type="text/html; charset=utf-8",body=html))
        self.page.goto(url)

    def test_amazon_search_ignores_crossed_out_prices(self):
        self.serve(amazon_card(),"https://www.amazon.com.br/s?k=lapis")
        offers,count,unreadable=read_cards(self.page,"amazon",QUERY)
        self.assertEqual((count,unreadable),(1,0))
        self.assertEqual(offers[0]["unit_cents"],1692)
        self.assertIsNone(offers[0]["shipping_cents"])

    def test_mercadolivre_search_deduplicates_nested_cards(self):
        self.serve(ml_card(),"https://lista.mercadolivre.com.br/lapis")
        offers,count,unreadable=read_cards(self.page,"mercadolivre",QUERY)
        self.assertEqual((count,unreadable),(1,0))
        self.assertEqual(offers[0]["unit_cents"],1590)

    def test_403_from_one_store_suspends_automatic_selection(self):
        def serve(route):
            is_ml="mercadolivre" in route.request.url
            route.fulfill(status=403 if is_ml else 200,content_type="text/html; charset=utf-8",body="Bloqueado" if is_ml else amazon_card())
        self.context.route("**/*",serve)
        with patch.object(type(self.page),"wait_for_timeout",return_value=None):
            result=collect(self.page,QUERY,["amazon","mercadolivre"],1)
        self.assertEqual(result.coverage["mercadolivre"]["status"],"blocked")
        self.assertEqual(result.offers[0]["unit_cents"],1692)
        self.assertIsNone(select_lowest(result))

    def test_pagination_is_bounded_and_duplicates_do_not_inflate_offers(self):
        visits=[]
        def serve(route):
            visits.append(route.request.url)
            second="page=2" in route.request.url
            body=amazon_card(1500 if second else 1692) + '<a class="s-pagination-next" href="/s?page=2">Seguinte</a>'
            route.fulfill(content_type="text/html; charset=utf-8",body=body)
        self.context.route("**/*",serve)
        with patch.object(type(self.page),"wait_for_timeout",return_value=None):
            result=collect(self.page,QUERY,["amazon"],2)
        self.assertEqual(len(visits),2)
        self.assertEqual(len(result.offers),1)
        self.assertEqual(result.offers[0]["unit_cents"],1500)

    def test_unreadable_matching_offer_prevents_false_lowest_price(self):
        self.serve(amazon_card().replace('<span class="a-price-fraction">92</span>',''),"https://www.amazon.com.br/s")
        with patch.object(type(self.page),"wait_for_timeout",return_value=None):
            result=collect(self.page,QUERY,["amazon"],1)
        self.assertEqual(result.coverage["amazon"]["status"],"incomplete")
        self.assertIsNone(select_lowest(result))

    def test_legacy_search_only_opens_mercadolivre_when_it_is_cheaper(self):
        import price_tracker
        result=SearchResult([offer("mercadolivre",1500,"MLB123456"),offer(price=1692)],{"amazon":{"status":"ok"},"mercadolivre":{"status":"ok"}})
        body=f"""<h1 class="ui-pdp-title">{QUERY}</h1><div class="ui-pdp-price__second-line"><span class="andes-money-amount">
<span class="andes-money-amount__currency-symbol">R$</span><span class="andes-money-amount__fraction">15</span>
<span class="andes-money-amount__cents">00</span></span></div>"""
        self.context.route("**/*",lambda route:route.fulfill(content_type="text/html; charset=utf-8",body=body))
        def prepare(page,offer,quantity):
            self.assertEqual(offer["key"],"MLB123456")
            self.assertEqual(quantity,2)
            page.close()
        settings=SearchSettings(QUERY,1800,60,("amazon","mercadolivre"),1)
        with patch("playwright.sync_api.sync_playwright") as factory, patch("price_tracker.collect",return_value=result), \
             patch("price_tracker.history_store",return_value=(None,None)), patch("shopping_runner.open_store_product",side_effect=prepare) as chosen, \
             patch("price_tracker.prepare_amazon") as amazon:
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value=self.context
            price_tracker.run(settings,2)
            chosen.assert_called_once()
            amazon.assert_not_called()

    def test_other_seller_quotes_are_recorded_as_starting_prices(self):
        self.serve(f"""<div data-component-type="s-search-result" data-asin="B0778W8ZV8">
<a href="/dp/B0778W8ZV8"><h2>{QUERY}</h2></a>
<p>Ver opções</p><p>Nenhuma opção de compra em destaque</p><p>R$ 13,80(10 ofertas de novos)</p></div>""",
            "https://www.amazon.com.br/s")
        offers,count,unreadable=read_cards(self.page,"amazon",QUERY)
        self.assertEqual(unreadable,0)
        self.assertEqual(offers[0]["unit_cents"],1380)
        self.assertEqual(offers[0]["offer_kind"],"other_sellers")

    def test_other_seller_ambiguous_values_are_not_used(self):
        self.serve(f"""<div data-component-type="s-search-result" data-asin="B0778W8ZV8">
<a href="/dp/B0778W8ZV8"><h2>{QUERY}</h2></a><p>Nenhuma opção de compra em destaque</p>
<p>R$ 13,80(10 ofertas de novos) R$ 1,00</p></div>""","https://www.amazon.com.br/s")
        offers,count,unreadable=read_cards(self.page,"amazon",QUERY)
        self.assertEqual(offers,[])
        self.assertEqual(unreadable,1)

    def test_explicitly_out_of_stock_cards_do_not_fabricate_a_price(self):
        self.serve(f"""<div data-component-type="s-search-result" data-asin="B0778W8ZV8">
<h2>{QUERY}</h2><p>Atualmente indisponível</p></div>""","https://www.amazon.com.br/s")
        offers,count,unreadable=read_cards(self.page,"amazon",QUERY)
        self.assertEqual((offers,unreadable),([],0))

    def test_final_review_requires_identity_quantity_and_total_with_shipping(self):
        from mercadolivre import MLSettings, checkout_review
        url="https://www.mercadolivre.com.br/lapis/p/MLB123456"
        self.serve(f"""<div data-testid="checkout-items"><a href="{url}">Lápis</a><span>Quantidade: 2</span></div>
<div data-testid="order-total">R$ 34,84</div><button onclick="window.paid=true">Confirmar compra</button>""","https://www.mercadolivre.com.br/checkout")
        settings=MLSettings("MLB123456",1800,60,url)
        review=checkout_review(self.page,settings,2)
        self.assertEqual(review.total_cents,3484)
        self.assertIsNone(self.page.evaluate("window.paid"))
        self.page.locator('[data-testid="order-total"]').evaluate("e=>e.textContent='R$ 36,01'")
        with self.assertRaises(CannotVerify):
            checkout_review(self.page,settings,2)


    def test_mercadolivre_prepares_cart_once_and_never_confirms_payment(self):
        import mercadolivre as ml
        state={"added":0}
        url="https://www.mercadolivre.com.br/lapis/p/MLB123456"
        def serve(route):
            current=route.request.url
            if current.endswith("/added"):
                state["added"]+=1
                body="Adicionado"
            elif "/gz/cart/" in current:
                body="Seu carrinho está vazio" if not state["added"] else f"""<div class="cart-item" data-quantity="2">
<a href="{url}">Lápis</a></div><button onclick="location.href='/checkout'">Continuar a compra</button>"""
            elif current.endswith("/checkout"):
                body=f"""<div data-testid="checkout-items"><a href="{url}">Lápis</a><span>Quantidade: 2</span></div>
<div data-testid="order-total">R$ 34,84</div><button onclick="window.paid=true">Confirmar compra</button>"""
            else:
                body="""<select name="quantity"><option>1</option><option>2</option></select>
<button onclick="location.href='/added'">Adicionar ao carrinho</button>"""
            route.fulfill(content_type="text/html; charset=utf-8",body=body)
        self.context.route("**/*",serve)
        self.page.goto(url)
        def tick(milliseconds):
            self.assertIsNone(self.page.evaluate("window.paid"))
            self.page.close()
        with patch.object(type(self.page),"wait_for_timeout",side_effect=tick), patch("bot.notify"):
            ml.prepare(self.page,self.context,ml.MLSettings("MLB123456",1800,60,url),2)
        self.assertEqual(state["added"],1)


if __name__=="__main__":
    unittest.main()
