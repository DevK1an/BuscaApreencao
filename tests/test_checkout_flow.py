"""Fluxo direto e etapas anteriores à confirmação, sem compras reais."""
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[1] / ".browsers"))

from playwright.sync_api import sync_playwright
import bot
from amazon import advance_checkout, is_order_confirmation
from policy import Settings
from test_amazon import ASIN, PRODUCT, review_html


class CheckoutFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def serve(self, html):
        self.context.route("**/*", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body=html
        ))
        self.page.goto("https://www.amazon.com.br/checkout")

    def test_uses_selected_address_and_payment_then_leaves_final_button_untouched(self):
        urls = []
        def serve(route):
            url = route.request.url
            urls.append(url)
            if url.endswith("/address"):
                html = """<input name="shipToThisAddress" type="button"
onclick="location.href='/payment'">"""
            elif url.endswith("/payment"):
                html = """<input name="ppw-widgetEvent:SetPaymentPlanSelectContinueEvent" type="button"
onclick="location.href='/checkout'">"""
            else:
                html = review_html()
            route.fulfill(content_type="text/html; charset=utf-8", body=html)
        self.context.route("**/*", serve)
        self.page.goto("https://www.amazon.com.br/address")
        states = []
        def tick(milliseconds):
            title = self.page.locator("#amazon-bot-status strong").inner_text()
            states.append(title)
            if title == "Pronto para sua confirmação":
                self.assertIsNone(self.page.evaluate("window.purchased"))
                self.page.close()
            elif len(states) > 5:
                self.fail("O fluxo não chegou à revisão final.")
        with patch.object(type(self.page), "wait_for_timeout", side_effect=tick), \
             patch("builtins.input", side_effect=AssertionError("Sem pausas no terminal")), \
             patch("bot.notify"), patch("sys.stdout", new_callable=io.StringIO):
            bot.final_review(self.page, Settings(ASIN, 450000, 120), 2)
        self.assertEqual(urls, [
            "https://www.amazon.com.br/address", "https://www.amazon.com.br/payment",
            "https://www.amazon.com.br/checkout",
        ])
        self.assertIn("Pronto para sua confirmação", states)

    def test_rechecks_total_and_revokes_approval_if_shipping_changes(self):
        self.serve(review_html())
        states = []
        def tick(milliseconds):
            states.append(self.page.locator("#amazon-bot-status strong").inner_text())
            if len(states) == 1:
                self.page.locator("#subtotals-marketplace-table tr").last.locator("td").last.evaluate(
                    "(el) => el.textContent = 'R$ 9.000,01'"
                )
            else:
                self.assertIsNone(self.page.evaluate("window.purchased"))
                self.page.close()
        with patch.object(type(self.page), "wait_for_timeout", side_effect=tick), \
             patch("bot.notify"), patch("sys.stdout", new_callable=io.StringIO):
            bot.final_review(self.page, Settings(ASIN, 450000, 120), 2)
        self.assertEqual(states, ["Pronto para sua confirmação", "Pedido ainda não validado"])

    def test_address_button_is_not_retried_after_validation_failure(self):
        self.serve('<input type="button" name="shipToThisAddress" onclick="window.clicks=(window.clicks||0)+1">')
        attempted = set()
        self.assertTrue(advance_checkout(self.page, attempted))
        self.assertFalse(advance_checkout(self.page, attempted))
        self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_final_order_button_is_never_an_automatic_continue(self):
        self.serve(review_html() + '<input name="shipToThisAddress" type="button" onclick="window.changed=true">')
        self.assertFalse(advance_checkout(self.page, set()))
        self.assertIsNone(self.page.evaluate("window.purchased"))
        self.assertIsNone(self.page.evaluate("window.changed"))

    def test_existing_exact_cart_is_reused_without_another_addition(self):
        self.context.route("**/*", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body=(
                f"""<div id="sc-active-cart"><div data-asin="{ASIN}"
data-itemtype="active" data-quantity="2">Produto</div></div>
<input type="button" name="proceedToRetailCheckout"
onclick="location.href='/checkout'">"""
                if "/gp/cart/" in route.request.url else
                review_html() if "/checkout" in route.request.url else PRODUCT
            ),
        ))
        def finish(page, settings, quantity):
            self.assertEqual(page.url, "https://www.amazon.com.br/checkout")
            page.close()
        with patch("playwright.sync_api.sync_playwright") as factory, \
             patch("bot.add_to_cart") as add, \
             patch("bot.final_review", side_effect=finish) as review, \
             patch("builtins.input", side_effect=AssertionError("Sem pausas no terminal")), \
             patch("sys.stdout", new_callable=io.StringIO):
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = self.context
            bot.run(Settings(ASIN, 450000, 120), 2)
            add.assert_not_called()
            review.assert_called_once()

    def test_login_wait_continues_without_enter(self):
        self.serve('<input id="ap_email">')
        def finish_login(milliseconds):
            self.page.locator("#ap_email").evaluate("(el) => el.remove()")
        with patch.object(type(self.page), "wait_for_timeout", side_effect=finish_login), \
             patch("builtins.input", side_effect=AssertionError("Sem pausas no terminal")), \
             patch("sys.stdout", new_callable=io.StringIO):
            bot.wait_for_login(self.page)
        self.assertEqual(self.page.locator("#ap_email").count(), 0)

    def test_confirmation_requires_amazon_thankyou_url(self):
        self.serve(review_html())
        self.assertFalse(is_order_confirmation(self.page))
        self.page.goto("https://www.amazon.com.br/gp/buy/thankyou/handlers/display.html")
        self.assertTrue(is_order_confirmation(self.page))
        self.page.goto("https://example.com/gp/buy/thankyou/handlers/display.html")
        self.assertFalse(is_order_confirmation(self.page))


if __name__ == "__main__":
    unittest.main()
