"""Reproduz inclusão assíncrona que é cancelada ao sair cedo do produto."""
import io
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from app_paths import browser_environment
from policy import Settings
from test_amazon import ASIN, PRODUCT, URL, review_html

browser_environment()


class AsyncCartTests(unittest.TestCase):
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
        self.state={"clicked":0,"added":0,"ready":False}
        self.page.on("console",lambda message:self.state.__setitem__("clicked",self.state["clicked"]+1) if message.text=="ADD_CLICKED" else None)
        self.context.route("**/*",self.serve)
    def tearDown(self):
        self.context.close()
    def serve(self,route):
        url=route.request.url
        if url.endswith("/delayed-add"):
            self.state["added"]+=1
            route.fulfill(content_type="application/json",body='{"ok":true}')
            return
        if "/checkout" in url:
            body=review_html()
        elif "/gp/cart/" in url:
            body=('<div id="sc-active-cart">Seu carrinho está vazio</div>' if not self.state["added"] else
                  f'<div id="sc-active-cart"><div data-asin="{ASIN}" data-itemtype="active" data-quantity="2">Produto</div></div><input type="button" name="proceedToRetailCheckout" onclick="location.href=\'/checkout\'">')
        else:
            body=PRODUCT.replace('window.added = (window.added || 0) + 1',
                 "console.log('ADD_CLICKED'); setTimeout(()=>fetch('/delayed-add',{method:'POST'}),400)")
        route.fulfill(content_type="text/html; charset=utf-8",body=body)
    def status(self,page,title,detail="",tone="info"):
        from status import show_status
        show_status(page,title,detail,tone)
        if title=="Pronto para sua confirmação":
            self.assertIsNone(page.evaluate("window.purchased"))
            self.state["ready"]=True
            page.close()
        elif title=="Preparação interrompida":
            page.close()

    def test_direct_flow_waits_for_server_cart_before_leaving_product(self):
        import bot
        with patch("playwright.sync_api.sync_playwright") as factory,patch("bot.show_status",side_effect=self.status), \
             patch("bot.notify"),patch("sys.stdout",new_callable=io.StringIO):
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value=self.context
            bot.run(Settings(ASIN,450000,60),2)
        self.assertTrue(self.state["ready"],"A navegação cancelou a inclusão antes de o carrinho confirmar o item.")
        self.assertEqual(self.state["clicked"],1)
        self.assertEqual(self.state["added"],1)

    def test_search_preparation_waits_for_server_cart_before_leaving_product(self):
        import price_tracker
        self.page.goto(URL)
        from runtime import wait
        with patch("bot.show_status",side_effect=self.status),patch("bot.notify"),patch("sys.stdout",new_callable=io.StringIO), \
             patch("bot.wait",side_effect=lambda page,ms: None if page.is_closed() else wait(page,ms)):
            price_tracker.prepare_amazon(self.page,self.context,Settings(ASIN,450000,60),2)
        self.assertTrue(self.state["ready"])
        self.assertEqual(self.state["clicked"],1)
        self.assertEqual(self.state["added"],1)

    def test_wait_timeout_keeps_product_open_without_repeating_add(self):
        from amazon import add_to_cart, wait_for_cart_add
        from policy import CannotVerify
        self.page.goto(URL)
        self.page.locator("#add-to-cart-button").evaluate("el=>el.setAttribute('onclick', \"console.log('ADD_CLICKED')\")")
        add_to_cart(self.page)
        with self.assertRaisesRegex(CannotVerify,"carrinho continua vazio"):
            wait_for_cart_add(self.page,ASIN,2,timeout_ms=400)
        self.assertEqual(self.state["clicked"],1)
        self.assertEqual(self.state["added"],0)
        self.assertEqual(self.page.url,URL)
        self.assertEqual(len(self.context.pages),1)

    def test_verification_does_not_replace_another_cart_item(self):
        from amazon import wait_for_cart_add
        from policy import CannotVerify
        self.context.route("**/gp/cart/**",lambda route:route.fulfill(content_type="text/html",
            body='<div id="sc-active-cart"><div data-asin="B000000001" data-itemtype="active" data-quantity="1">Outro produto</div></div>'))
        self.page.goto(URL)
        with self.assertRaisesRegex(CannotVerify,"B000000001 × 1"):
            wait_for_cart_add(self.page,ASIN,2,timeout_ms=400)
        self.assertEqual(self.state["clicked"],0)
        self.assertEqual(self.state["added"],0)

    def test_verification_requires_the_requested_quantity(self):
        from amazon import wait_for_cart_add
        from policy import CannotVerify
        self.context.route("**/gp/cart/**",lambda route:route.fulfill(content_type="text/html",
            body=f'<div id="sc-active-cart"><div data-asin="{ASIN}" data-itemtype="active" data-quantity="1">Produto</div></div>'))
        self.page.goto(URL)
        with self.assertRaisesRegex(CannotVerify,"Esperado:.*× 2; encontrado:.*× 1"):
            wait_for_cart_add(self.page,ASIN,2,timeout_ms=400)
        self.assertEqual(self.state["clicked"],0)

    def test_stop_prevents_probe_and_any_extra_addition(self):
        from amazon import wait_for_cart_add
        from runtime import BotStopped
        self.page.goto(URL)
        with patch("runtime.check_stop",side_effect=BotStopped),self.assertRaises(BotStopped):
            wait_for_cart_add(self.page,ASIN,2)
        self.assertEqual(len(self.context.pages),1)
        self.assertEqual(self.state["clicked"],0)
