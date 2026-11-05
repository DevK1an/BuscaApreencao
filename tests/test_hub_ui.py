"""Testa o hub no Chromium com processos falsos; não abre nem compra na Amazon."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from playwright.sync_api import expect, sync_playwright

from hub import HubServer, JobManager
from hub_store import ProductStore
from test_hub import FakeProcess, OTHER_URL, product

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))


class HubUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProductStore(Path(self.temp.name) / "products.json")
        self.first = self.store.save(product(name="Meu console", quantity=1))
        self.manager = JobManager(self.store, self.temp.name, popen=FakeProcess)
        self.server = HubServer(("127.0.0.1", 0), self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.context = self.browser.new_context(viewport={"width":1440,"height":1050}, locale="pt-BR")
        self.page = self.context.new_page()
        self.errors = []
        self.external = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        def local_only(route):
            if route.request.url.startswith(self.server.origin + "/"):
                route.continue_()
            else:
                self.external.append(route.request.url)
                route.abort()
        self.context.route("**/*", local_only)
        self.page.goto(self.server.origin)
        expect(self.page.locator(".product-row")).to_have_count(1)

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.manager.shutdown()
        self.thread.join(timeout=2)
        self.temp.cleanup()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.external, [])

    def screenshot(self, filename):
        if os.environ.get("HUB_SCREENSHOT") == "1":
            path = ROOT / "artifacts" / filename
            path.parent.mkdir(exist_ok=True)
            self.page.screenshot(path=str(path), full_page=True)

    def add_product(self):
        self.page.get_by_role("button", name="Adicionar produto", exact=True).click()
        self.page.locator("#product-url").fill(OTHER_URL + "?tag=tracking")
        self.page.locator("#product-name").fill("Fone sem fio")
        self.page.locator("#product-quantity").fill("3")
        self.page.locator("#product-price").fill("399,90")
        expect(self.page.locator("#budget-preview")).to_contain_text("1.199,70")
        self.page.get_by_role("button", name="Salvar produto", exact=True).click()
        expect(self.page.locator(".product-row")).to_have_count(2)

    def test_complete_crud_start_status_and_stop_from_hub(self):
        self.add_product()
        row = self.page.locator(".product-row").filter(has_text="Fone sem fio")
        row.locator("[data-quantity]").fill("4")
        row.locator("[data-quantity]").press("Tab")
        expect(row.locator(".price-cell small")).to_contain_text("1.599,60")
        row.get_by_role("button", name="Editar Fone sem fio", exact=True).click()
        self.page.locator("#product-name").fill("Fone para viagens")
        self.page.locator("#product-price").fill("349,90")
        self.page.get_by_role("button", name="Salvar alterações", exact=True).click()
        row = self.page.locator(".product-row").filter(has_text="Fone para viagens")
        expect(row.locator(".price-cell strong")).to_contain_text("349,90")
        self.page.reload()
        expect(self.page.locator(".product-row")).to_have_count(2)
        row = self.page.locator(".product-row").filter(has_text="Fone para viagens")
        expect(row.locator("[data-quantity]")).to_have_value("4")
        first = self.page.locator(".product-row").filter(has_text="Meu console")
        first.get_by_role("button", name="Iniciar", exact=True).click()
        expect(first.get_by_role("button", name="Parar", exact=True)).to_be_visible()
        expect(row.get_by_role("button", name="Iniciar", exact=True)).to_be_disabled()
        expect(first.get_by_role("button", name="Editar Meu console", exact=True)).to_be_disabled()
        job = self.manager.jobs[self.first["id"]]
        job["event_file"].write_text(json.dumps({
            "state":"ready", "title":"Pronto para sua confirmação",
            "detail":"1 unidade. Total com frete: R$ 4.490,00. Confirme pessoalmente na Amazon."
        }), encoding="utf-8")
        expect(self.page.locator("#execution-title")).to_contain_text("Pronto para sua confirmação", timeout=7000)
        expect(self.page.locator("#stat-ready")).to_have_text("1")
        self.screenshot("hub-desktop.png")
        job["log_handle"].write("Mensagem simulada do bot\n")
        job["log_handle"].flush()
        self.page.get_by_role("button", name="Ver atividade", exact=True).click()
        expect(self.page.locator("#activity-log")).to_contain_text("Mensagem simulada do bot")
        self.page.get_by_role("button", name="Fechar atividade", exact=True).click()
        first.get_by_role("button", name="Parar", exact=True).click()
        expect(first.locator(".badge")).to_have_text("Parado")
        expect(row.get_by_role("button", name="Iniciar", exact=True)).to_be_enabled()
        row.get_by_role("button", name="Excluir Fone para viagens", exact=True).click()
        expect(self.page.locator(".product-row")).to_have_count(1)
        self.page.reload()
        expect(self.page.locator(".product-row")).to_have_count(1)

    def test_invalid_link_stays_in_form_and_product_names_are_rendered_as_text(self):
        self.page.get_by_role("button", name="Adicionar produto", exact=True).click()
        self.page.locator("#product-url").fill("https://127.0.0.1/dp/B000000001")
        self.page.get_by_role("button", name="Salvar produto", exact=True).click()
        expect(self.page.locator("#form-error")).to_contain_text("loja pública")
        expect(self.page.locator("#product-dialog")).to_be_visible()
        self.page.get_by_role("button", name="Cancelar", exact=True).click()
        payload = '<img src=x onerror="window.hacked=1">'
        self.store.save(product(OTHER_URL, name=payload))
        self.page.reload()
        expect(self.page.locator(".product-row")).to_have_count(2)
        expect(self.page.locator(".product-name").filter(has_text=payload)).to_be_visible()
        self.assertIsNone(self.page.evaluate("window.hacked"))
        self.assertEqual(self.page.locator(".product-row img").count(), 0)

    def test_mobile_layout_and_help(self):
        self.add_product()
        self.page.set_viewport_size({"width":390, "height":844})
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.screenshot("hub-mobile.png")
        self.page.get_by_role("button", name="Adicionar produto", exact=True).click()
        expect(self.page.locator("#product-dialog")).to_be_visible()
        self.page.get_by_role("button", name="Cancelar", exact=True).click()
        self.page.set_viewport_size({"width":1440,"height":1050})
        self.page.get_by_role("button", name="Como funciona", exact=False).click()
        expect(self.page.locator("#help-title")).to_have_text("Do link à sua confirmação")
        self.page.get_by_role("button", name="Fechar ajuda", exact=True).click()


if __name__ == "__main__":
    unittest.main()
