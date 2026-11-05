"""Reproduz o preço real da Amazon com a-offscreen vazio e valores separados."""
import os
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

from amazon import product_price
from policy import CannotVerify

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))
ASIN = "B0778W8ZV8"

SPLIT_PRICE = """<span class="a-price aok-align-center reinventPricePriceToPayMargin priceToPay apex-pricetopay-value">
<span class="a-offscreen"> </span><span aria-hidden="true">
<span class="a-price-symbol">R$</span><span class="a-price-whole">16<span class="a-price-decimal">,</span></span>
<span class="a-price-fraction">92</span></span></span>"""

def html(price=SPLIT_PRICE):
    return f"""<h1 id="productTitle">Lápis</h1><input id="ASIN" value="{ASIN}">
<div id="corePriceDisplay_desktop_feature_div">{price}
<span class="a-price a-text-price apex-basisprice-value" data-a-strike="true">
<span class="a-offscreen">R$19,90</span></span></div>
<input type="button" id="add-to-cart-button" value="Adicionar">
<div>Recomendado: R$ 1,00</div>"""


class PriceReadingTests(unittest.TestCase):
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
        self.content = ""
        self.context.route("**/*", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body=self.content
        ))
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def load(self, content):
        self.content = content
        self.page.goto(f"https://www.amazon.com.br/dp/{ASIN}")

    def test_empty_accessibility_price_uses_same_offer_whole_and_fraction(self):
        self.load(html())
        self.assertEqual(product_price(self.page, ASIN), 1692)

    def test_clipped_accessibility_span_and_thousands(self):
        price = SPLIT_PRICE.replace("> </span>", ">R$ 4.499,99</span>", 1)
        price = price.replace(">16<span", ">4.499<span").replace(">92</span>", ">99</span>")
        self.load('<style>.a-offscreen{position:absolute;clip:rect(1px,1px,1px,1px);height:1px;width:1px;overflow:hidden}</style>' + html(price))
        self.assertEqual(product_price(self.page, ASIN), 449999)

    def test_new_apex_price_class_without_price_to_pay(self):
        self.load(html(SPLIT_PRICE.replace(" priceToPay ", " ")))
        self.assertEqual(product_price(self.page, ASIN), 1692)

    def test_hidden_old_price_and_sponsored_price_are_ignored(self):
        old = SPLIT_PRICE.replace(">16<span", ">1<span")
        self.load(html('<div style="display:none">' + old + '</div>' + SPLIT_PRICE))
        self.assertEqual(product_price(self.page, ASIN), 1692)

    def test_visible_and_accessibility_price_must_agree(self):
        self.load(html(SPLIT_PRICE.replace("> </span>", ">R$ 1,00</span>", 1)))
        with self.assertRaises(CannotVerify):
            product_price(self.page, ASIN)

    def test_partial_parts_unknown_currency_or_multiple_prices_do_not_pass(self):
        for price in (
            SPLIT_PRICE.replace('<span class="a-price-fraction">92</span>', ""),
            SPLIT_PRICE.replace(">R$</span>", ">US$</span>"),
            SPLIT_PRICE + SPLIT_PRICE.replace(">16<span", ">99<span"),
            SPLIT_PRICE.replace(">92</span>", ">9</span>"),
        ):
            with self.subTest(price=price):
                self.load(html(price))
                with self.assertRaises(CannotVerify):
                    product_price(self.page, ASIN)

    def test_whitespace_in_parts_and_zero_cents(self):
        price = SPLIT_PRICE.replace(">16<span", "> 1.000 <span").replace(">92</span>", "> 00 </span>")
        self.load(html(price))
        self.assertEqual(product_price(self.page, ASIN), 100000)

    def test_installment_and_crossed_out_prices_do_not_replace_missing_offer_price(self):
        self.load(html(SPLIT_PRICE.replace(">16<span", "><span").replace(">92</span>", "></span>")))
        with self.assertRaises(CannotVerify):
            product_price(self.page, ASIN)


if __name__ == "__main__":
    unittest.main()
