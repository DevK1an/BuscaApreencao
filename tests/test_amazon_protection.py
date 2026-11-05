"""Recusa de seguro opcional sem aceitar cobertura nem repetir a inclusão."""
import io
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from app_paths import browser_environment
from amazon import add_to_cart, wait_for_cart_add
from amazon_protection import decline_protection_offers
from policy import Settings
from test_amazon import ASIN, PRODUCT, URL, review_html

browser_environment()

PROMPT = """<div id="attach-desktop-sideSheet" role="dialog" hidden>
<div id="attach-warranty-pane"><h2>Adicionar ao seu pedido</h2>
<p>Cobrir este produto: Garantia Estendida contra falhas e defeitos de 12 meses R$ 21,21</p>
<span id="attachSiNoCoverage" tabindex="0" onclick="window.declined=(window.declined||0)+1;this.closest('[role=dialog]').hidden=true;fetch('/complete-add',{method:'POST'})"><span> \n  Não, obrigado \n </span></span>
<button id="attachSiAddCoverage" onclick="window.insured=true">Incluir proteção</button>
</div></div>"""


class InsuranceTests(unittest.TestCase):
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
        self.state={"add":0,"commit":0,"ready":False}
        self.page.on("console",lambda message:self.state.__setitem__("add",self.state["add"]+1) if message.text=="ADD_CLICKED" else None)
        self.context.route("**/*",self.serve)
    def tearDown(self):
        self.context.close()
    def serve(self,route):
        url=route.request.url
        if url.endswith("/complete-add"):
            self.state["commit"]+=1
            route.fulfill(content_type="application/json",body='{"ok":true}')
            return
        if "/checkout" in url:
            body=review_html()
        elif "/gp/cart/" in url:
            body=('<div id="sc-active-cart">Seu carrinho está vazio</div>' if not self.state["commit"] else
                  f'<div id="sc-active-cart"><div data-asin="{ASIN}" data-itemtype="active" data-quantity="2">Produto</div></div><input type="button" name="proceedToRetailCheckout" onclick="location.href=\'/checkout\'">')
        else:
            body=PRODUCT.replace("window.added = (window.added || 0) + 1",
                                 "console.log('ADD_CLICKED');document.querySelector('#attach-desktop-sideSheet').hidden=false")+PROMPT
        route.fulfill(content_type="text/html; charset=utf-8",body=body)
    def load(self,body,url=URL):
        self.context.unroute("**/*")
        self.context.route("**/*",lambda route:route.fulfill(content_type="text/html; charset=utf-8",body=body))
        self.page.goto(url)
    def status(self,page,title,detail="",tone="info"):
        from status import show_status
        show_status(page,title,detail,tone)
        if title=="Pronto para sua confirmação":
            self.assertIsNone(page.evaluate("window.purchased"))
            self.state["ready"]=True
            page.close()
        elif title=="Preparação interrompida":
            page.close()

    def test_actual_side_sheet_text_declines_before_cart_verification(self):
        self.page.goto(URL)
        add_to_cart(self.page)
        with patch("sys.stdout",new_callable=io.StringIO):
            wait_for_cart_add(self.page,ASIN,2,timeout_ms=5000)
        self.assertEqual(self.state["add"],1)
        self.assertEqual(self.state["commit"],1)
        self.assertEqual(self.page.evaluate("window.declined"),1)
        self.assertIsNone(self.page.evaluate("window.insured"))
        self.assertIsNone(self.page.evaluate("window.purchased"))

    def test_direct_flow_reaches_manual_review_after_declining(self):
        import bot
        with patch("playwright.sync_api.sync_playwright") as factory,patch("bot.show_status",side_effect=self.status), \
             patch("bot.notify"),patch("sys.stdout",new_callable=io.StringIO):
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value=self.context
            bot.run(Settings(ASIN,450000,60),2)
        self.assertTrue(self.state["ready"])
        self.assertEqual((self.state["add"],self.state["commit"]),(1,1))

    def test_search_flow_reaches_manual_review_after_declining(self):
        import price_tracker
        from runtime import wait
        self.page.goto(URL)
        with patch("bot.show_status",side_effect=self.status),patch("bot.notify"),patch("sys.stdout",new_callable=io.StringIO), \
             patch("bot.wait",side_effect=lambda page,ms:None if page.is_closed() else wait(page,ms)):
            price_tracker.prepare_amazon(self.page,self.context,Settings(ASIN,450000,60),2)
        self.assertTrue(self.state["ready"])
        self.assertEqual((self.state["add"],self.state["commit"]),(1,1))

    def test_prompt_that_remains_visible_is_not_clicked_twice(self):
        self.load(PROMPT.replace(" hidden","").replace("window.declined=(window.declined||0)+1;this.closest('[role=dialog]').hidden=true;fetch('/complete-add',{method:'POST'})","window.declined=(window.declined||0)+1"))
        attempted=set()
        self.assertTrue(decline_protection_offers(self.page,attempted))
        self.assertFalse(decline_protection_offers(self.page,attempted))
        self.assertEqual(self.page.evaluate("window.declined"),1)
        self.assertIsNone(self.page.evaluate("window.insured"))

    def test_unrelated_no_thanks_and_final_order_buttons_are_untouched(self):
        self.load("""<div role="dialog">Receber ofertas?<button onclick="window.other=true">Não, obrigado</button></div>
<div id="attach-warranty-pane"><h2>Garantia estendida</h2>
<button id="attachSiAddCoverage" onclick="window.insured=true">Não, obrigado</button>
<button name="placeYourOrder1" onclick="window.purchased=true">Não, obrigado</button></div>""")
        self.assertFalse(decline_protection_offers(self.page,set()))
        for key in ("other","insured","purchased"):
            self.assertIsNone(self.page.evaluate("window."+key))

    def test_cancel_existing_insurance_is_not_an_offer_to_add_protection(self):
        self.load("""<div role="dialog">Deseja cancelar o seguro atual?
<button onclick="window.keep=true">Não, obrigado</button></div>""")
        self.assertFalse(decline_protection_offers(self.page,set()))
        self.assertIsNone(self.page.evaluate("window.keep"))

    def test_same_origin_frame_offer_is_declined(self):
        prompt=PROMPT.replace(" hidden","").replace("window.declined=(window.declined||0)+1;this.closest('[role=dialog]').hidden=true;fetch('/complete-add',{method:'POST'})",
                         "window.declined=1;this.closest('[role=dialog]').hidden=true")
        self.context.unroute("**/*")
        self.context.route("**/*",lambda route:route.fulfill(content_type="text/html; charset=utf-8",
            body=prompt if route.request.url.endswith("/insurance") else '<iframe src="/insurance"></iframe>'))
        self.page.goto(URL)
        frame=self.page.frames[1]
        frame.locator("#attachSiNoCoverage").wait_for()
        self.assertTrue(decline_protection_offers(self.page,set()))
        self.assertEqual(frame.evaluate("window.declined"),1)
        self.assertIsNone(frame.evaluate("window.insured"))

    def test_foreign_frame_offer_is_not_automated(self):
        prompt=PROMPT.replace(" hidden","")
        self.context.unroute("**/*")
        self.context.route("**/*",lambda route:route.fulfill(content_type="text/html; charset=utf-8",
            body=prompt if "foreign.example" in route.request.url else '<iframe src="https://foreign.example/insurance"></iframe>'))
        self.page.goto(URL)
        self.page.frames[1].locator("#attachSiNoCoverage").wait_for()
        self.assertFalse(decline_protection_offers(self.page,set()))

    def test_popup_from_product_can_decline_without_accepting(self):
        self.page.goto(URL)
        prompt=PROMPT.replace(" hidden","").replace("window.declined=(window.declined||0)+1;this.closest('[role=dialog]').hidden=true;fetch('/complete-add',{method:'POST'})",
                         "window.declined=1;this.closest('[role=dialog]').hidden=true")
        self.context.route("**/insurance-popup",lambda route:route.fulfill(content_type="text/html; charset=utf-8",body=prompt))
        with self.page.expect_popup() as popup_event:
            self.page.evaluate("window.open('/insurance-popup')")
        popup=popup_event.value
        popup.locator("#attachSiNoCoverage").wait_for()
        self.assertTrue(decline_protection_offers(self.page,set()))
        self.assertEqual(popup.evaluate("window.declined"),1)
        self.assertIsNone(popup.evaluate("window.insured"))


    def test_amazon_wrapper_declines_when_input_overlays_the_label(self):
        prompt = PROMPT.replace(" hidden","")
        prompt = prompt.replace('<span> \n  Não, obrigado \n </span>',
            '<span style="display:inline-block;position:relative;width:190px;height:45px">'
            '<input type="button" aria-hidden="true" tabindex="-1" style="position:absolute;inset:0;width:100%;height:100%;opacity:0">'
            '<span>Não, obrigado</span></span>')
        prompt = prompt.replace("window.declined=(window.declined||0)+1;this.closest('[role=dialog]').hidden=true;fetch('/complete-add',{method:'POST'})",
                                "window.declined=1;this.closest('[role=dialog]').hidden=true")
        self.load(prompt)
        self.assertTrue(decline_protection_offers(self.page,set()))
        self.assertEqual(self.page.evaluate("window.declined"),1)
        self.assertIsNone(self.page.evaluate("window.insured"))
