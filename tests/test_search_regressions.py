"""Regressões da busca Switch 2 e da seleção com comparação parcial."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from hub_store import ProductStore, validate_product
from market_search import SearchResult, collect, eligible_offers, read_cards, select_lowest
from offer_matching import matches
from price_history import PriceHistory
from price_tracker import SearchSettings
from test_tracking import QUERY, amazon_card, offer
from test_price_reading import html as product_html


class MatchingRegressionTests(unittest.TestCase):
    def test_switch_console_does_not_match_games_or_old_v2(self):
        for title in (
            "Super Mario Galaxy + Super Mario Galaxy 2 - Nintendo Switch",
            "Kirby Air Riders - Nintendo Switch 2",
            "Mario Kart World - Nintendo Switch 2",
            "Jogo Nintendo Switch 2 Donkey Kong Bananza",
            "Console Nintendo Switch V2 - 32GB + 2 Controles",
            "Nintendo Switch 32GB V2",
            "Capa Nintendo Switch 2",
        ):
            with self.subTest(title=title):
                self.assertFalse(matches("Nintendo Switch 2",title))

    def test_switch_console_accepts_hardware_and_explicit_bundles(self):
        for title in (
            "Console Nintendo Switch 2 Internacional",
            "Console Nintendo Switch 2 Nacional 256GB",
            "Nintendo Switch 2",
            "Nintendo Switch 2 256GB Preto",
            "Nintendo Switch 2 + Mario Kart World Bundle",
            "Bundle Nintendo Switch 2 + Mario Kart World",
            "Console Nintendo Switch 2 + Jogo Mario Kart World",
        ):
            with self.subTest(title=title):
                self.assertTrue(matches("Nintendo Switch 2",title))
        self.assertTrue(matches("Console Nintendo Switch 2","Nintendo Switch 2"))

    def test_named_games_remain_searchable(self):
        self.assertTrue(matches("Kirby Air Riders Nintendo Switch 2","Kirby Air Riders - Nintendo Switch 2"))
        self.assertTrue(matches("Jogo Nintendo Switch 2 Kirby","Jogo Kirby Nintendo Switch 2"))
        self.assertFalse(matches("Nintendo Switch 2","Kirby Air Riders Nintendo Switch 2"))

    def test_partial_policy_persists_and_migrates_existing_hub_products(self):
        data={"mode":"search","query":QUERY,"quantity":1,"max_price":"18,00"}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"products.json"
            store=ProductStore(path)
            item=store.save(data)
            self.assertTrue(item["allow_partial"])
            legacy=json.loads(path.read_text(encoding="utf-8"))
            legacy[0].pop("allow_partial")
            path.write_text(json.dumps(legacy),encoding="utf-8")
            self.assertTrue(ProductStore(path).get(item["id"])["allow_partial"])
            store.save({**data,"allow_partial":False},item["id"])
            self.assertFalse(ProductStore(path).get(item["id"])["allow_partial"])
        with self.assertRaises(ValueError):
            validate_product({**data,"allow_partial":"false"})

    def test_failed_store_without_successful_pages_does_not_supply_candidates(self):
        amazon=offer(price=1600)
        failed=offer("mercadolivre",100,"MLB123456")
        result=SearchResult([failed,amazon],{
            "amazon":{"status":"ok"},"mercadolivre":{"status":"blocked"},
        })
        self.assertIsNone(select_lowest(result))
        chosen=select_lowest(result,allow_partial=True)
        self.assertEqual(chosen["key"],amazon["key"])
        self.assertTrue(chosen["selection_partial"])
        self.assertIsNone(chosen["shipping_cents"])
        self.assertNotIn("selection_partial",amazon)
        for status in ("error","unsupported","blocked"):
            result.coverage["mercadolivre"]["status"]=status
            self.assertEqual(len(eligible_offers(result,True)),1)

    def test_partial_selection_uses_readable_quotes_from_incomplete_results(self):
        result=SearchResult([offer()],{"amazon":{"status":"incomplete"}})
        self.assertIsNone(select_lowest(result))
        self.assertEqual(select_lowest(result,True)["unit_cents"],1692)
        result.coverage["amazon"]["status"]="blocked"
        self.assertIsNone(select_lowest(result,True))

    def test_old_game_prices_are_filtered_from_console_history_without_rewriting_it(self):
        game={**offer(price=40745),"title":"Super Mario Galaxy 2 - Nintendo Switch"}
        console={**offer(price=359010),"key":"B000000002","title":"Console Nintendo Switch 2 Internacional"}
        with tempfile.TemporaryDirectory() as folder:
            history=PriceHistory(Path(folder)/"prices.sqlite3")
            history.record("switch",[game,console],{"amazon":{"status":"ok"}},selected=game,query="Nintendo Switch 2")
            filtered=history.get("switch",query="Nintendo Switch 2")
            self.assertEqual(filtered["latest"]["offers"],[console])
            self.assertIsNone(filtered["latest"]["selected"])
            self.assertEqual(filtered["series"][0]["min_unit_cents"],359010)
            self.assertEqual(len(history.get("switch")["latest"]["offers"]),2)


class SearchRegressionTests(unittest.TestCase):
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
    def tearDown(self):
        self.context.close()

    def test_unreadable_v2_and_cheap_game_do_not_pollute_console_results(self):
        console=amazon_card(359010).replace(QUERY,"Console Nintendo Switch 2 Internacional")
        v2=amazon_card().replace(QUERY,"Console Nintendo Switch V2 + 2 controles").replace("B0778W8ZV8","B000000002")
        v2=v2.replace('<span class="a-price-fraction">92</span>',"")
        game=amazon_card(40745).replace(QUERY,"Super Mario Galaxy 2 - Nintendo Switch").replace("B0778W8ZV8","B000000003")
        self.context.route("**/*",lambda route:route.fulfill(content_type="text/html; charset=utf-8",body=console+v2+game))
        with patch.object(type(self.page),"wait_for_timeout",return_value=None):
            result=collect(self.page,"Nintendo Switch 2",["amazon"],1)
        self.assertEqual(result.coverage["amazon"]["status"],"ok")
        self.assertEqual(result.coverage["amazon"]["unreadable"],0)
        self.assertEqual(result.coverage["amazon"]["compatible"],1)
        self.assertEqual(result.coverage["amazon"]["filtered"],2)
        self.assertEqual(select_lowest(result)["unit_cents"],359010)

    def test_amazon_can_prepare_when_ml_blocks_and_partial_is_enabled(self):
        import price_tracker
        result=SearchResult([offer(),offer("mercadolivre",100,"MLB123456")],{
            "amazon":{"status":"ok","detail":""},
            "mercadolivre":{"status":"blocked","detail":"HTTP 403"},
        })
        body=product_html().replace('<h1 id="productTitle">Lápis</h1>',f'<h1 id="productTitle">{QUERY}</h1>')
        self.context.route("**/*",lambda route:route.fulfill(content_type="text/html; charset=utf-8",body=body))
        def prepare(page,context,settings,quantity):
            self.assertEqual(settings.asin,"B0778W8ZV8")
            self.assertEqual(quantity,2)
            page.close()
        settings=SearchSettings(QUERY,1800,60,("amazon","mercadolivre"),1,allow_partial=True)
        with patch("playwright.sync_api.sync_playwright") as factory, patch("price_tracker.collect",return_value=result), \
             patch("price_tracker.history_store",return_value=(None,None)), patch("price_tracker.prepare_amazon",side_effect=prepare) as chosen, \
             patch("mercadolivre.prepare") as ml, patch("price_tracker.emit_status") as event, patch("sys.stdout",new_callable=io.StringIO):
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value=self.context
            price_tracker.run(settings,2)
            chosen.assert_called_once()
            ml.assert_not_called()
            messages=[str(call) for call in event.call_args_list]
            self.assertTrue(any("Compara" in message and "parcial" in message for message in messages))

    def test_verified_pages_remain_available_after_later_page_blocks_or_fails(self):
        for fail in ("http","network"):
            with self.subTest(fail=fail):
                self.context.unroute("**/*")
                def serve(route):
                    if "mercadolivre" in route.request.url:
                        route.fulfill(status=403,body="Bloqueado")
                    elif "page=2" in route.request.url:
                        if fail=="http":
                            route.fulfill(status=503,body="Indisponível")
                        else:
                            route.abort()
                    else:
                        route.fulfill(content_type="text/html; charset=utf-8",body=amazon_card()+'<a class="s-pagination-next" href="/s?page=2">Próxima</a>')
                self.context.route("**/*",serve)
                with patch.object(type(self.page),"wait_for_timeout",return_value=None):
                    result=collect(self.page,QUERY,["amazon","mercadolivre"],2)
                self.assertEqual(result.coverage["amazon"]["pages"],1)
                self.assertEqual(result.coverage["amazon"]["priced"],1)
                self.assertEqual(result.coverage["amazon"]["status"],"blocked" if fail=="http" else "error")
                self.assertIsNone(select_lowest(result))
                chosen=select_lowest(result,True)
                self.assertEqual(chosen["key"],"B0778W8ZV8")
                self.assertTrue(chosen["selection_partial"])
