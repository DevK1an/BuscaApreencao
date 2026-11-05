"""Testes de contrato em Chromium, com rede Amazon substituída por HTML local."""
import os
from pathlib import Path
from unittest.mock import patch
import io
import unittest

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[1] / ".browsers"))

from playwright.sync_api import Error, sync_playwright

from amazon import (
    CART_URL, add_to_cart, cart_items, checkout_review, open_checkout,
    product_price, require_exact_cart, set_quantity,
)
from policy import CannotVerify, Settings, check_review

ASIN = "B0HK2KVYD7"
URL = f"https://www.amazon.com.br/dp/{ASIN}"

PRODUCT = f"""
<h1 id="productTitle">Produto de teste</h1>
<input id="ASIN" value="{ASIN}">
<div id="corePriceDisplay_desktop_feature_div">
  <span class="priceToPay"><span class="a-offscreen">R$ 4.490,00</span></span>
  <del>R$ 5.000,00</del>
</div>
<select id="quantity"><option>1</option><option>2</option></select>
<input type="button" id="add-to-cart-button" value="Adicionar ao carrinho"
 onclick="window.added = (window.added || 0) + 1">
<input type="button" id="buy-now-button" value="Comprar agora"
 onclick="window.purchased = true">
"""

def review_html(total="R$ 9.000,00", quantity=2, asin=ASIN, final_button=True):
    return f"""
<div id="spc-orders">
 <a href="/dp/{asin}">Produto</a><span>Quantidade: {quantity}</span>
</div>
<table id="subtotals-marketplace-table">
 <tr><td>Itens:</td><td>R$ 8.980,00</td></tr>
 <tr><td>Frete e manuseio:</td><td>R$ 20,00</td></tr>
 <tr><td>Total do pedido:</td><td>{total}</td></tr>
</table>
{'<input type="button" name="placeYourOrder1" onclick="window.purchased=true" value="Confirmar pedido">' if final_button else ''}
"""


class AmazonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.html = ""
        self.context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=self.html
        ))
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def load(self, html, url=URL):
        self.html = html
        self.page.goto(url)

    def test_main_price_ignores_list_and_recommendation_prices(self):
        self.load(PRODUCT + '<div>Patrocinado: R$ 1,00</div>')
        self.assertEqual(product_price(self.page, ASIN), 449000)

    def test_unavailable_or_wrong_product_is_rejected(self):
        for html in [
            PRODUCT.replace(ASIN, "B000000000"),
            PRODUCT.replace('id="add-to-cart-button"', 'id="unavailable"'),
            PRODUCT + '<input id="captchacharacters">',
        ]:
            self.load(html)
            with self.assertRaises(CannotVerify):
                product_price(self.page, ASIN)

    def test_quantity_is_never_silently_reduced(self):
        self.load(PRODUCT)
        set_quantity(self.page, 2)
        self.assertEqual(self.page.locator("#quantity").input_value(), "2")
        with self.assertRaises(CannotVerify):
            set_quantity(self.page, 3)

    def test_cart_checks_exact_quantity_and_ignores_saved_items(self):
        self.load(f"""
<div id="sc-active-cart"><div data-asin="{ASIN}" data-itemtype="active" data-quantity="2">Produto</div></div>
<div id="sc-saved-cart"><div data-asin="B000000000">Salvo</div></div>
""", CART_URL)
        require_exact_cart(self.page, ASIN, 2)
        with self.assertRaises(CannotVerify):
            require_exact_cart(self.page, ASIN, 1)

    def test_extra_active_cart_item_is_rejected(self):
        self.load(f"""
<div id="sc-active-cart">
 <div data-asin="{ASIN}" data-itemtype="active" data-quantity="2">Produto</div>
 <div data-asin="B000000000" data-itemtype="active" data-quantity="1">Outro</div>
</div>
""", CART_URL)
        with self.assertRaises(CannotVerify):
            require_exact_cart(self.page, ASIN, 2)

    def test_unknown_cart_is_not_assumed_empty(self):
        self.load('<div id="sc-active-cart">Layout novo</div>', CART_URL)
        with self.assertRaises(CannotVerify):
            cart_items(self.page)

    def test_known_empty_cart(self):
        self.load('<div id="sc-active-cart">Seu carrinho está vazio</div>', CART_URL)
        self.assertEqual(cart_items(self.page), [])

    def test_final_total_includes_shipping_and_never_clicks_payment(self):
        self.load(review_html())
        review = checkout_review(self.page)
        self.assertEqual(review.total_cents, 900000)
        check_review(review, Settings(ASIN, 450000, 120), 2)
        self.assertIsNone(self.page.evaluate("window.purchased"))

    def test_shipping_can_push_total_over_budget(self):
        self.load(review_html(total="R$ 9.000,01"))
        with self.assertRaises(CannotVerify):
            check_review(checkout_review(self.page), Settings(ASIN, 450000, 120), 2)

    def test_subtotal_missing_total_and_incomplete_checkout_cannot_pass(self):
        for html in [
            review_html().replace("Total do pedido:", "Subtotal:"),
            review_html(final_button=False),
            review_html().replace("R$ 9.000,00", "A calcular"),
            review_html().replace("Quantidade: 2", ""),
        ]:
            self.load(html)
            with self.assertRaises(CannotVerify):
                checkout_review(self.page)

    def test_changed_quantity_or_product_cannot_pass(self):
        for html in (review_html(quantity=1), review_html(asin="B000000000")):
            self.load(html)
            with self.assertRaises(CannotVerify):
                check_review(checkout_review(self.page), Settings(ASIN, 450000, 120), 2)

    def test_only_add_and_checkout_actions_are_automated(self):
        self.load(PRODUCT)
        add_to_cart(self.page)
        self.assertEqual(self.page.evaluate("window.added"), 1)
        self.assertIsNone(self.page.evaluate("window.purchased"))
        self.load("""
<input type="button" name="proceedToRetailCheckout" onclick="window.checkout=true">
<input type="button" name="placeYourOrder1" onclick="window.purchased=true">
""", CART_URL)
        open_checkout(self.page)
        self.assertTrue(self.page.evaluate("window.checkout"))
        self.assertIsNone(self.page.evaluate("window.purchased"))


    def test_network_failure_after_add_does_not_duplicate_or_open_checkout(self):
        import bot
        self.context.unroute("**/*")
        self.context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body='<div id="sc-active-cart">Seu carrinho está vazio</div>'
            if "/gp/cart/" in route.request.url else PRODUCT
        ))

        def interrupted_add(page):
            add_to_cart(page)
            raise Error("Conexão interrompida após adicionar")

        with patch("playwright.sync_api.sync_playwright") as factory, \
             patch("builtins.input", side_effect=AssertionError("Não deve pedir Enter")), \
             patch("bot.show_status", side_effect=lambda page, *args: page.close()), \
             patch("bot.add_to_cart", side_effect=interrupted_add) as add, \
             patch("bot.open_checkout") as checkout, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = self.context
            bot.run(Settings(ASIN, 450000, 120), 2)
            self.assertEqual(add.call_count, 1)
            checkout.assert_not_called()
            self.assertIn("Nenhuma nova adição", output.getvalue())

    def test_waits_for_restock_then_prepares_without_extra_prompts(self):
        import bot
        state = {"added": 0, "product_reads": 0, "urls": []}
        self.context.unroute("**/*")

        def serve(route):
            url = route.request.url
            state["urls"].append(url)
            if "/checkout" in url:
                html = review_html()
            elif "/gp/cart/" in url and state["added"]:
                html = f"""<div id="sc-active-cart">
<div data-asin="{ASIN}" data-itemtype="active" data-quantity="2">Produto</div>
</div><input type="button" name="proceedToRetailCheckout"
onclick="location.href='https://www.amazon.com.br/checkout'">"""
            elif "/gp/cart/" in url:
                html = '<div id="sc-active-cart">Seu carrinho está vazio</div>'
            else:
                state["product_reads"] += 1
                html = PRODUCT if state["product_reads"] > 4 else PRODUCT.replace('id="add-to-cart-button"', 'id="out-of-stock"')
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

        def tracked_add(page):
            add_to_cart(page)
            state["added"] += 1

        def status_and_close(page, title, detail="", tone="info"):
            from status import show_status
            show_status(page, title, detail, tone)
            if title == "Pronto para sua confirmação":
                self.assertIsNone(page.evaluate("window.purchased"))
                page.close()

        self.context.route("**/*", serve)
        with patch("playwright.sync_api.sync_playwright") as factory, \
             patch("builtins.input", side_effect=AssertionError("Não deve pedir Enter")) as prompts, \
             patch("bot.show_status", side_effect=status_and_close), \
             patch.object(type(self.page), "wait_for_timeout", return_value=None), \
             patch("bot.add_to_cart", side_effect=tracked_add), \
             patch("bot.notify"), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = self.context
            bot.run(Settings(ASIN, 450000, 120), 2)
            self.assertEqual(state["added"], 1)
            self.assertEqual(state["product_reads"], 5)
            self.assertEqual(prompts.call_count, 0)
            self.assertEqual(state["urls"][0], URL)
            self.assertIn("PRONTO PARA SUA CONFIRMAÇÃO", output.getvalue())


if __name__ == "__main__":
    unittest.main()
