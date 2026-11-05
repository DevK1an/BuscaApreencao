import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from playwright.sync_api import expect, sync_playwright

from app_paths import browser_environment
from hub import HubServer, JobManager
from hub_store import ProductStore
from test_hub import FakeProcess, product
from test_tracking import QUERY, offer

browser_environment()


class TrackingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=ProductStore(Path(self.temp.name)/"products.json")
        self.store.save(product())
        self.manager=JobManager(self.store,self.temp.name,popen=FakeProcess)
        self.server=HubServer(("127.0.0.1",0),self.manager)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.context=self.browser.new_context(viewport={"width":1440,"height":1050},locale="pt-BR")
        self.page=self.context.new_page()
        self.errors=[]
        self.page.on("pageerror",lambda error:self.errors.append(str(error)))
        self.context.route("**/*",lambda route:route.continue_() if route.request.url.startswith(self.server.origin+"/") else route.abort())
        self.page.goto(self.server.origin)
        expect(self.page.locator(".product-row")).to_have_count(1)
    def tearDown(self):
        self.context.close()
        self.server.shutdown(); self.server.server_close()
        self.manager.shutdown(); self.thread.join(timeout=2)
        self.temp.cleanup()
        self.assertEqual(self.errors,[])

    def test_query_configuration_auto_selection_history_and_mobile(self):
        self.page.get_by_role("button",name="Rastrear preços",exact=True).click()
        expect(self.page.locator("#product-mode")).to_have_value("search")
        expect(self.page.locator("#url-fields")).to_be_hidden()
        self.page.locator("#product-query").fill(QUERY)
        self.page.locator("#product-price").fill("18,00")
        self.page.get_by_role("button",name="Salvar produto",exact=True).click()
        expect(self.page.locator(".product-row")).to_have_count(2)
        row=self.page.locator(".product-row").filter(has_text=QUERY)
        row.locator("[data-quantity]").fill("2")
        row.locator("[data-quantity]").press("Tab")
        expect(row.locator(".price-cell small")).to_contain_text("36,00")
        row.get_by_role("button",name="Iniciar",exact=True).click()
        expect(row.get_by_role("button",name="Parar",exact=True)).to_be_visible()
        saved=next(p for p in self.store.list() if p.get("mode")=="search")
        job=self.manager.jobs[saved["id"]]
        command=job["process"].command
        config=json.loads(Path(command[command.index("--config")+1]).read_text(encoding="utf-8"))
        self.assertEqual(config["mode"],"search")
        self.assertEqual(config["marketplaces"],["google_shopping"])
        self.assertEqual(config["excluded_stores"],["AliExpress","Shopee"])
        self.assertEqual(config["pages_per_store"],30)
        self.manager.history.record(saved["id"],[offer(price=1800)],{"amazon":{"status":"ok","pages":1}},query=QUERY)
        winner=offer("mercadolivre",1499,"MLB123456")
        self.manager.history.record(saved["id"],[offer(),winner],{
            "amazon":{"status":"ok","pages":3},"mercadolivre":{"status":"ok","pages":2},
        },selected=winner,query=QUERY,note="Menor preço anunciado encontrado. Frete a confirmar no checkout.")
        row.get_by_role("button",name="Ofertas e histórico",exact=False).click()
        expect(self.page.locator("#price-offers .offer-row")).to_have_count(2)
        expect(self.page.locator("#price-offers .chosen")).to_contain_text("Mercado Livre")
        expect(self.page.locator("#price-offers .chosen")).to_contain_text("14,99")
        expect(self.page.locator("#price-chart circle")).to_have_count(2)
        expect(self.page.locator("#price-offers")).to_contain_text("Frete a confirmar")
        if os.environ.get("HUB_SCREENSHOT")=="1":
            target=Path(__file__).resolve().parents[1]/"artifacts"/"rastreamento.png"
            target.parent.mkdir(exist_ok=True)
            self.page.screenshot(path=str(target),full_page=True)
        self.page.set_viewport_size({"width":390,"height":844})
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.page.get_by_role("button",name="Fechar preços",exact=True).click()
        self.page.reload()
        expect(self.page.locator(".product-row").filter(has_text=QUERY)).to_be_visible()

    def test_old_poll_response_cannot_overwrite_new_quantity(self):
        row=self.page.locator(".product-row")
        self.page.wait_for_function("!refreshing")
        queued=[]
        def delay(route):
            response=route.fetch()
            queued.append((route,response))
        self.page.route("**/api/state",delay)
        self.page.evaluate("() => { refresh(); }")
        for _ in range(20):
            if queued:
                break
            self.page.wait_for_timeout(25)
        self.assertTrue(queued)
        row.get_by_role("button",name="Aumentar quantidade de Meu console",exact=True).click()
        expect(row.locator("[data-quantity]")).to_have_value("3")
        route,response=queued.pop(0)
        route.fulfill(response=response)
        self.page.wait_for_timeout(100)
        expect(row.locator("[data-quantity]")).to_have_value("3")
        self.assertEqual(self.store.list()[0]["quantity"],3)

    def test_poll_does_not_start_during_product_save(self):
        self.page.wait_for_function("!refreshing")
        queued=[]
        reads=[]
        def hold_save(route):
            queued.append(route)
        self.page.route("**/api/products",hold_save)
        self.page.route("**/api/state",lambda route:(reads.append(route.request.url),route.continue_()))
        self.page.get_by_role("button",name="Rastrear preços",exact=True).click()
        self.page.locator("#product-query").fill(QUERY)
        self.page.locator("#product-price").fill("18,00")
        self.page.get_by_role("button",name="Salvar produto",exact=True).click()
        self.page.wait_for_function("busy")
        before=len(reads)
        self.page.evaluate("() => refresh()")
        self.assertEqual(len(reads),before)
        self.assertEqual(len(queued),1)
        response=queued[0].fetch()
        queued[0].fulfill(response=response)
        expect(self.page.locator(".product-row")).to_have_count(2)

    def test_partial_coverage_is_visible_and_has_no_selected_offer(self):
        saved=self.store.list()[0]
        self.manager.history.record(saved["id"],[offer()],{
            "amazon":{"status":"ok","pages":1},
            "mercadolivre":{"status":"blocked","pages":0,"detail":"HTTP 403"},
        },note="Busca parcial; escolha automática suspensa.")
        row=self.page.locator(".product-row")
        row.get_by_role("button",name="Ofertas e histórico",exact=False).click()
        expect(self.page.locator("#prices-note")).to_contain_text("suspensa")
        expect(self.page.locator("#prices-coverage")).to_contain_text("Acesso bloqueado")
        expect(self.page.locator("#price-offers .chosen")).to_have_count(0)


    def test_partial_choice_persists_after_edit_quantity_and_worker_start(self):
        self.page.get_by_role("button",name="Rastrear preços",exact=True).click()
        expect(self.page.locator("#product-partial")).to_be_checked()
        self.page.locator("#product-query").fill(QUERY)
        self.page.locator("#product-price").fill("18,00")
        self.page.locator("#product-partial").uncheck()
        self.page.get_by_role("button",name="Salvar produto",exact=True).click()
        row=self.page.locator(".product-row").filter(has_text=QUERY)
        expect(row).to_be_visible()
        row.get_by_role("button",name="Aumentar quantidade de "+QUERY,exact=True).click()
        expect(row.locator("[data-quantity]")).to_have_value("2")
        self.page.reload()
        row.get_by_role("button",name="Editar "+QUERY,exact=True).click()
        expect(self.page.locator("#product-partial")).not_to_be_checked()
        self.page.locator("#product-partial").check()
        self.page.get_by_role("button",name="Salvar alterações",exact=True).click()
        row.get_by_role("button",name="Iniciar",exact=True).click()
        expect(row.get_by_role("button",name="Parar",exact=True)).to_be_visible()
        saved=next(p for p in self.store.list() if p.get("mode")=="search")
        command=self.manager.jobs[saved["id"]]["process"].command
        config=json.loads(Path(command[command.index("--config")+1]).read_text(encoding="utf-8"))
        self.assertTrue(config["allow_partial"])

    def test_available_offer_is_marked_selected_in_partial_comparison(self):
        saved=self.store.list()[0]
        winner={**offer(),"selection_partial":True}
        self.manager.history.record(saved["id"],[offer()],{
            "amazon":{"status":"ok","pages":1,"priced":1},
            "mercadolivre":{"status":"blocked","pages":0,"detail":"HTTP 403"},
        },selected=winner,note="Comparação parcial: ofertas disponíveis.")
        self.page.locator(".product-row").get_by_role("button",name="Ofertas e histórico",exact=False).click()
        expect(self.page.locator("#prices-note")).to_contain_text("Comparação parcial")
        expect(self.page.locator("#prices-coverage")).to_contain_text("Acesso bloqueado")
        expect(self.page.locator("#price-offers .chosen")).to_contain_text("Escolhida na comparação parcial")


    def assert_exclusions_reach_worker(self, excluded):
        self.page.get_by_role("button",name="Rastrear preços",exact=True).click()
        expect(self.page.get_by_role("group",name="Origem da busca")).to_be_visible()
        expect(self.page.locator("#search-source")).to_contain_text("Google Shopping")
        self.page.locator("#product-query").fill(QUERY)
        self.page.locator("#product-price").fill("18,00")
        self.page.locator("#product-excluded").fill("\n".join(excluded))
        self.page.get_by_role("button",name="Salvar produto",exact=True).click()
        row=self.page.locator(".product-row").filter(has_text=QUERY)
        expect(row).to_be_visible()
        saved=next(p for p in self.store.list() if p.get("mode")=="search")
        self.assertEqual(saved["excluded_stores"],excluded)
        expect(row.locator(".product-code")).to_contain_text(str(len(excluded))+" lojas excluídas")
        row.get_by_role("button",name="Aumentar quantidade de "+QUERY,exact=True).click()
        expect(row.locator("[data-quantity]")).to_have_value("2")
        self.page.reload()
        row.get_by_role("button",name="Editar "+QUERY,exact=True).click()
        expect(self.page.locator("#product-excluded")).to_have_value("\n".join(excluded))
        self.page.get_by_role("button",name="Salvar alterações",exact=True).click()
        row.get_by_role("button",name="Iniciar",exact=True).click()
        expect(row.get_by_role("button",name="Parar",exact=True)).to_be_visible()
        command=self.manager.jobs[saved["id"]]["process"].command
        config=json.loads(Path(command[command.index("--config")+1]).read_text(encoding="utf-8"))
        self.assertEqual(config["marketplaces"],["google_shopping"])
        self.assertEqual(config["search_provider"],"google_shopping")
        self.assertEqual(config["excluded_stores"],excluded)

    def test_default_exclusions_are_visible_on_creation(self):
        self.page.get_by_role("button",name="Rastrear preços",exact=True).click()
        expect(self.page.locator("#product-excluded")).to_have_value("AliExpress\nShopee")

    def test_custom_store_names_reach_worker(self):
        self.assert_exclusions_reach_worker(["AliExpress","Shopee","Loja Azul"])

    def test_custom_store_domains_reach_worker(self):
        self.assert_exclusions_reach_worker(["aliexpress.com","shopee.com.br"])

    def test_removing_all_exclusions_allows_any_store(self):
        self.assert_exclusions_reach_worker([])

    def test_direct_link_action_and_exclusions_survive_mode_switch(self):
        self.page.get_by_role("button",name="Adicionar produto",exact=True).click()
        self.page.locator("#product-url").fill("https://www.amazon.com.br/dp/B000000001")
        expect(self.page.locator("#stores-hint")).to_contain_text("prepara o carrinho")
        expect(self.page.locator("#product-excluded")).to_be_hidden()
        self.page.locator("#product-mode").select_option("search")
        expect(self.page.locator("#product-excluded")).to_be_visible()
        self.page.locator("#product-excluded").fill("Shopee\nLoja Azul")
        self.page.locator("#product-mode").select_option("url")
        self.page.locator("#product-url").fill("https://lojaverde.com.br/produto")
        expect(self.page.locator("#stores-hint")).to_contain_text("somente abre")
        self.page.locator("#product-mode").select_option("search")
        expect(self.page.locator("#product-excluded")).to_have_value("Shopee\nLoja Azul")
        self.page.set_viewport_size({"width":390,"height":844})
        self.assertTrue(self.page.evaluate("document.querySelector('#product-dialog').scrollWidth <= document.querySelector('#product-dialog').clientWidth"))

if __name__=="__main__":
    unittest.main()
