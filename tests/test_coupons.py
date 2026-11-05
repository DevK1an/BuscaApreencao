"""Cupons simulados: ativação única, preço final e confirmação pessoal."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from app_paths import browser_environment
from coupons import CouponApplier, activate_coupons
from policy import CannotVerify, Settings
from runtime import BotStopped
from test_amazon import ASIN, PRODUCT, URL, review_html

browser_environment()

AMAZON_COUPON = """
<div id="promoPriceBlockMessage_feature_div">
<i class="newCouponBadge">Cupom:</i>
<label for="checkboxpctch21375110640230222"></label>
<input id="checkboxpctch21375110640230222" type="checkbox"
 onchange="window.clips=(window.clips||0)+1">
<span class="couponLabelText">Aplicar Cupom de 11% Termos</span>
</div>"""


class CouponTests(unittest.TestCase):
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
        self.body=""
        self.context.route("**/*",lambda route:route.fulfill(content_type="text/html; charset=utf-8",body=self.body))
    def tearDown(self):
        self.context.close()
    def serve(self,body,url=URL):
        self.body=body
        self.page.goto(url)

    def test_current_amazon_checkbox_is_selected_once(self):
        self.serve(PRODUCT+AMAZON_COUPON)
        applier=CouponApplier("amazon")
        self.assertTrue(applier.apply(self.page))
        self.assertFalse(applier.apply(self.page))
        self.assertTrue(self.page.locator("input[id^='checkboxpctch']").is_checked())
        self.assertEqual(self.page.evaluate("window.clips"),1)
        self.assertIsNone(self.page.evaluate("window.added"))
        self.assertIsNone(self.page.evaluate("window.purchased"))

    def test_already_selected_coupon_is_never_toggled_off(self):
        self.serve(AMAZON_COUPON.replace('type="checkbox"','type="checkbox" checked'))
        self.assertFalse(CouponApplier("amazon").apply(self.page))
        self.assertIsNone(self.page.evaluate("window.clips"))
        self.assertTrue(self.page.locator("input[type='checkbox']").is_checked())

    def test_hidden_checkbox_uses_its_visible_label_once(self):
        body=AMAZON_COUPON.replace('type="checkbox"','type="checkbox" style="display:none"')
        body=body.replace('for="checkboxpctch21375110640230222"></label>','for="checkboxpctch21375110640230222">Aplicar cupom</label>')
        self.serve(body)
        self.assertTrue(CouponApplier("amazon").apply(self.page))
        self.assertTrue(self.page.locator("input[type='checkbox']").is_checked())
        self.assertEqual(self.page.evaluate("window.clips"),1)

    def test_disabled_coupon_and_unrelated_controls_are_untouched(self):
        body=AMAZON_COUPON.replace('type="checkbox"','type="checkbox" disabled')
        body=body.replace('</div>','''<label><input id="warranty" type="checkbox">Contratar garantia</label>
<button onclick="window.purchased=true">Aplicar cupom e confirmar compra</button>
<button name="placeYourOrder1" onclick="window.purchased=true">Confirmar compra</button></div>''')
        self.serve(body)
        self.assertFalse(CouponApplier("amazon").apply(self.page))
        self.assertFalse(self.page.locator("#warranty").is_checked())
        self.assertIsNone(self.page.evaluate("window.purchased"))

    def test_coupons_in_recommendations_are_not_activated(self):
        self.serve('<aside id="rhf"><label><input id="coupon-other" type="checkbox">Cupom de outro produto</label></aside>'+PRODUCT)
        self.assertFalse(CouponApplier("amazon").apply(self.page))
        self.assertFalse(self.page.locator("#coupon-other").is_checked())

    def test_wrong_store_and_stop_signal_prevent_activation(self):
        self.serve(AMAZON_COUPON,url="https://example.com/dp/"+ASIN)
        with self.assertRaises(CannotVerify):
            CouponApplier("amazon").apply(self.page)
        self.serve(AMAZON_COUPON)
        with tempfile.TemporaryDirectory() as folder:
            stop=Path(folder)/"stop"
            stop.touch()
            with patch.dict("os.environ",{"AMAZON_BOT_STOP_FILE":str(stop)}),self.assertRaises(BotStopped):
                CouponApplier("amazon").apply(self.page)
        self.assertFalse(self.page.locator("input[type='checkbox']").is_checked())

    def test_coupon_needing_an_unknown_code_is_not_submitted_empty(self):
        self.serve('''<div data-testid="coupon-section">Cupom
<input type="text" placeholder="Código do cupom">
<button onclick="window.clicks=1">Aplicar</button></div>''')
        self.assertFalse(CouponApplier("amazon").apply(self.page))
        self.assertIsNone(self.page.evaluate("window.clicks"))

    def test_code_already_entered_in_store_can_be_applied(self):
        self.serve('''<div data-testid="coupon-section">Cupom
<input type="text" value="FORNECIDO-PELO-USUARIO">
<button onclick="window.clicks=(window.clicks||0)+1;this.parentElement.textContent='Cupom aplicado'">Aplicar</button></div>''')
        applier=CouponApplier("amazon")
        self.assertTrue(applier.apply(self.page))
        self.assertFalse(applier.apply(self.page))
        self.assertEqual(self.page.evaluate("window.clicks"),1)

    def test_unconfirmed_button_is_not_clicked_again(self):
        self.serve('''<div class="ui-pdp-coupons">Cupom
<button onclick="window.clicks=(window.clicks||0)+1">Resgatar cupom</button></div>''',
                   "https://www.mercadolivre.com.br/produto/p/MLB123456")
        applier=CouponApplier("mercadolivre")
        self.assertTrue(applier.apply(self.page))
        self.assertTrue(applier.apply(self.page))
        with patch("coupons.time.monotonic",return_value=applier.pending_started+9):
            for _ in range(2):
                with self.assertRaises(CannotVerify):
                    applier.apply(self.page)
        self.assertEqual(self.page.evaluate("window.clicks"),1)

    def test_rejected_coupon_is_not_retried_or_treated_as_a_discount(self):
        self.serve('''<div class="ui-pdp-coupons">Cupom
<button onclick="window.clicks=(window.clicks||0)+1;this.parentElement.innerHTML='Cupom expirado'">Resgatar cupom</button></div>''',
                   "https://www.mercadolivre.com.br/produto/p/MLB123456")
        applier=CouponApplier("mercadolivre")
        self.assertTrue(applier.apply(self.page))
        self.assertFalse(applier.apply(self.page))
        self.assertIn("indisponível",applier.message)
        self.assertEqual(self.page.evaluate("window.clicks"),1)

    def test_coupon_modal_does_not_repeat_the_button_that_opened_it(self):
        self.serve('''<div class="ui-pdp-coupons" id="product-coupon">Cupom
<button onclick="window.opens=(window.opens||0)+1;document.querySelector('#coupon-modal').hidden=false">Aplicar cupom</button></div>
<div id="coupon-modal" role="dialog" hidden>Seu cupom
<button onclick="document.querySelector('#product-coupon').textContent='Cupom aplicado';this.parentElement.remove()">Aplicar</button></div>''',
                   "https://www.mercadolivre.com.br/produto/p/MLB123456")
        applier=CouponApplier("mercadolivre")
        self.assertTrue(applier.apply(self.page))
        self.assertTrue(applier.apply(self.page))
        self.assertFalse(applier.apply(self.page))
        self.assertEqual(self.page.evaluate("window.opens"),1)

    def amazon_prepare_flow(self,search,limit=450000,total="R$ 9.000,00"):
        import bot
        import price_tracker
        state={"added":0}
        self.context.unroute("**/*")
        def serve(route):
            url=route.request.url
            if "/checkout" in url:
                body=review_html(total=total)
            elif "/gp/cart/" in url:
                body=('<div id="sc-active-cart">Seu carrinho está vazio</div>' if not state["added"] else
                      f'<div id="sc-active-cart"><div data-asin="{ASIN}" data-itemtype="active" data-quantity="2">Produto</div></div><input type="button" name="proceedToRetailCheckout" onclick="location.href=\'/checkout\'">')
            else:
                body=PRODUCT+AMAZON_COUPON
            route.fulfill(content_type="text/html; charset=utf-8",body=body)
        self.context.route("**/*",serve)
        self.page.goto(URL)
        def add(page):
            self.assertTrue(page.locator("input[id^='checkboxpctch']").is_checked())
            self.assertEqual(page.evaluate("window.clips"),1)
            self.assertIsNone(page.evaluate("window.purchased"))
            state["added"]+=1
        def show(page,title,detail="",tone="info"):
            from status import show_status
            show_status(page,title,detail,tone)
            if title=="Pronto para sua confirmação":
                self.assertIsNone(page.evaluate("window.purchased"))
                page.close()
        with patch("bot.add_to_cart",side_effect=add),patch("amazon.add_to_cart",side_effect=add), \
             patch("bot.show_status",side_effect=show),patch("bot.notify"), \
             patch.object(type(self.page),"wait_for_timeout",return_value=None),patch("sys.stdout",new_callable=io.StringIO):
            if search:
                price_tracker.prepare_amazon(self.page,self.context,Settings(ASIN,limit,60),2)
            else:
                with patch("playwright.sync_api.sync_playwright") as factory:
                    factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value=self.context
                    bot.run(Settings(ASIN,limit,60),2)
        self.assertEqual(state["added"],1)

    def test_direct_amazon_applies_coupon_before_adding(self):
        self.amazon_prepare_flow(False)

    def test_search_amazon_applies_coupon_before_adding(self):
        self.amazon_prepare_flow(True)

    def test_mercadolivre_applies_coupon_then_leaves_final_confirmation_untouched(self):
        import mercadolivre as ml
        state={"added":0}
        url="https://www.mercadolivre.com.br/lapis/p/MLB123456"
        self.context.unroute("**/*")
        def serve(route):
            current=route.request.url
            if current.endswith("/added"):
                state["added"]+=1
                body="Adicionado"
            elif "/gz/cart/" in current:
                body=("Seu carrinho está vazio" if not state["added"] else
                      f'<div class="cart-item" data-quantity="2"><a href="{url}">Produto</a></div><button onclick="location.href=\'/checkout\'">Continuar a compra</button>')
            elif current.endswith("/checkout"):
                body=f'<div data-testid="checkout-items"><a href="{url}">Produto</a><span>Quantidade: 2</span></div><div data-testid="order-total">R$ 34,84</div><button onclick="window.paid=true">Confirmar compra</button>'
            else:
                body='''<div class="ui-pdp-price__second-line"><span class="andes-money-amount"><span class="andes-money-amount__currency-symbol">R$</span><span class="andes-money-amount__fraction">16</span><span class="andes-money-amount__cents">92</span></span></div>
<select name="quantity"><option>1</option><option>2</option></select>
<div class="ui-pdp-coupons"><label><input id="coupon" type="checkbox">Aplicar cupom</label></div>
<button onclick="if(document.querySelector('#coupon').checked)location.href='/added'">Adicionar ao carrinho</button>'''
            route.fulfill(content_type="text/html; charset=utf-8",body=body)
        self.context.route("**/*",serve)
        self.page.goto(url)
        def tick(milliseconds):
            node=self.page.locator("#amazon-bot-status strong")
            if node.count() and node.inner_text()=="Pronto para sua confirmação":
                self.assertIsNone(self.page.evaluate("window.paid"))
                self.page.close()
        with patch.object(type(self.page),"wait_for_timeout",side_effect=tick),patch("bot.notify"),patch("sys.stdout",new_callable=io.StringIO):
            ml.prepare(self.page,self.context,ml.MLSettings("MLB123456",1800,60,url),2)
        self.assertEqual(state["added"],1)

    def final_discount_flow(self,late_increase):
        import bot
        coupon='''<div data-testid="coupon-section">Cupom
<button onclick="window.clicks=(window.clicks||0)+1;this.parentElement.textContent='Cupom aplicado'">Aplicar cupom</button></div>'''
        self.serve(review_html(total="R$ 9.100,00")+coupon,url="https://www.amazon.com.br/checkout")
        states=[]
        def tick(milliseconds):
            node=self.page.locator("#amazon-bot-status strong")
            states.append(node.inner_text() if node.count() else "")
            self.assertIsNone(self.page.evaluate("window.purchased"))
            if len(states)==1:
                self.page.locator("#subtotals-marketplace-table tr").last.locator("td").last.evaluate("el=>el.textContent='R$ 9.000,00'")
            elif len(states)==2 and late_increase:
                self.page.locator("#subtotals-marketplace-table tr").last.locator("td").last.evaluate("el=>el.textContent='R$ 9.000,01'")
            elif len(states)==3:
                self.assertEqual(self.page.evaluate("window.clicks"),1)
                self.page.close()
            elif len(states)>3:
                self.fail("O teste não terminou na revisão prevista.")
        with patch.object(type(self.page),"wait_for_timeout",side_effect=tick),patch("bot.notify"),patch("sys.stdout",new_callable=io.StringIO):
            bot.final_review(self.page,Settings(ASIN,450000,60),2)
        return states

    def test_final_total_is_rechecked_after_coupon_before_ready(self):
        states=self.final_discount_flow(False)
        self.assertNotIn("Pronto para sua confirmação",states[:2])
        self.assertEqual(states[-1],"Pronto para sua confirmação")

    def test_shipping_increase_after_coupon_keeps_order_above_limit_unapproved(self):
        states=self.final_discount_flow(True)
        self.assertNotIn("Pronto para sua confirmação",states)
        self.assertEqual(states[-1],"Pedido ainda não validado")

    def test_pending_coupon_is_not_forgotten_after_checkout_navigation(self):
        self.serve('<div data-testid="coupon-section">Cupom<button onclick="location.href=\'/checkout\'">Aplicar cupom</button></div>')
        applier=CouponApplier("amazon")
        self.assertTrue(applier.apply(self.page))
        self.assertTrue(self.page.url.endswith("/checkout"))
        # Sem uma confirmação da loja, navegar não transforma a tentativa em desconto.
        self.page.locator('[data-testid="coupon-section"]').evaluate("el=>el.remove()")
        self.assertTrue(applier.apply(self.page))
        with patch("coupons.time.monotonic",return_value=applier.pending_started+9),self.assertRaises(CannotVerify):
            applier.apply(self.page)

    def test_coupon_detection_does_not_invent_a_discount(self):
        from coupons import has_available_coupon
        self.serve(PRODUCT+AMAZON_COUPON)
        self.assertTrue(has_available_coupon(self.page,"amazon"))
        self.assertEqual(self.page.locator(".a-offscreen").first.inner_text(),"R$ 4.490,00")
        self.assertFalse(self.page.locator("input[type='checkbox']").is_checked())
        self.serve('<div data-testid="coupon-section">Cupom<input type="text"><button>Aplicar</button></div>')
        self.assertFalse(has_available_coupon(self.page,"amazon"))

    def test_search_card_coupon_is_only_an_availability_hint(self):
        from market_search import read_cards
        from test_tracking import QUERY, amazon_card
        self.serve(amazon_card(1800).replace("</div>","<span>Aplicar cupom de 10%</span></div>",1))
        offers,_,_=read_cards(self.page,"amazon",QUERY)
        self.assertTrue(offers[0]["coupon_available"])
        self.assertEqual(offers[0]["unit_cents"],1800)
        self.assertIsNone(offers[0]["shipping_cents"])

    def test_coupon_can_bring_direct_product_into_budget_only_at_final_review(self):
        self.amazon_prepare_flow(False,440000,"R$ 8.400,00")

    def test_coupon_can_bring_search_product_into_budget_only_at_final_review(self):
        self.amazon_prepare_flow(True,440000,"R$ 8.400,00")

    def test_search_can_check_coupon_offer_above_advertised_price_cap(self):
        import price_tracker
        from market_search import SearchResult
        from price_tracker import SearchSettings
        from test_tracking import QUERY, offer
        from test_price_reading import html
        candidate={**offer(price=1800),"coupon_available":True}
        result=SearchResult([candidate],{"amazon":{"status":"ok","detail":""}})
        body=html().replace('<h1 id="productTitle">Lápis</h1>',f'<h1 id="productTitle">{QUERY}</h1>')
        body=body.replace(">16<span",">18<span").replace(">92</span>",">00</span>")+AMAZON_COUPON
        self.context.unroute("**/*")
        self.context.route("**/*",lambda route:route.fulfill(content_type="text/html; charset=utf-8",body=body))
        def prepare(page,context,settings,quantity):
            self.assertEqual(settings.max_unit_cents,1700)
            self.assertEqual(quantity,1)
            page.close()
        with patch("playwright.sync_api.sync_playwright") as factory,patch("price_tracker.collect",return_value=result), \
             patch("price_tracker.history_store",return_value=(None,None)),patch("price_tracker.prepare_amazon",side_effect=prepare) as chosen, \
             patch("sys.stdout",new_callable=io.StringIO):
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value=self.context
            price_tracker.run(SearchSettings(QUERY,1700,60,("amazon",),1),1)
            chosen.assert_called_once()


    def test_final_order_identity_is_never_clicked_even_with_coupon_label(self):
        self.serve('''<div data-testid="coupon-section">Cupom
<button name="placeYourOrder1" onclick="window.purchased=true">Aplicar cupom</button>
<input type="submit" id="submitOrderButtonId" value="Aplicar" onclick="window.purchased=true">
</div>''')
        from coupons import has_available_coupon
        self.assertFalse(has_available_coupon(self.page,"amazon"))
        self.assertFalse(CouponApplier("amazon").apply(self.page))
        self.assertIsNone(self.page.evaluate("window.purchased"))

    def test_successful_coupon_with_timeout_still_rechecks_total_before_ready(self):
        import bot
        from playwright.sync_api import Error, Locator
        self.serve(review_html()+AMAZON_COUPON,url="https://www.amazon.com.br/checkout")
        node=self.page.locator("input[id^='checkboxpctch']")
        real_check=node.check
        def timeout_after_click(*args,**kwargs):
            real_check(*args,**kwargs)
            raise Error("Timeout após a loja selecionar o cupom")
        states=[]
        def tick(milliseconds):
            states.append(self.page.locator("#amazon-bot-status strong").inner_text())
            self.assertIsNone(self.page.evaluate("window.purchased"))
            if len(states)==3:
                self.page.close()
            if len(states)>3:
                self.fail("A revisão não terminou no momento esperado.")
        with patch.object(Locator,"check",side_effect=timeout_after_click) as check, \
             patch.object(type(self.page),"wait_for_timeout",side_effect=tick), \
             patch("bot.notify"),patch("sys.stdout",new_callable=io.StringIO):
            bot.final_review(self.page,Settings(ASIN,450000,60),2)
            check.assert_called_once()
        self.assertEqual(states,["Aplicando cupom","Aplicando cupom","Pronto para sua confirmação"])


    def test_delayed_coupon_confirmation_is_waited_without_a_second_click(self):
        self.serve('''<div class="ui-pdp-coupons">Cupom
<button onclick="window.clicks=(window.clicks||0)+1;setTimeout(()=>this.parentElement.textContent='Cupom aplicado',1200)">Resgatar cupom</button></div>''',
                   "https://www.mercadolivre.com.br/produto/p/MLB123456")
        with patch("sys.stdout",new_callable=io.StringIO):
            self.assertTrue(activate_coupons(self.page,"mercadolivre"))
        self.assertEqual(self.page.evaluate("window.clicks"),1)
        self.assertIn("Cupom aplicado",self.page.locator(".ui-pdp-coupons").inner_text())

    def test_selected_disabled_coupon_is_already_confirmed(self):
        self.serve(AMAZON_COUPON.replace('type="checkbox"','type="checkbox" checked disabled'))
        applier=CouponApplier("amazon")
        self.assertFalse(applier.apply(self.page))
        self.assertIn("selecionado",applier.message)
        self.assertIsNone(self.page.evaluate("window.clips"))

    def test_coupon_check_without_offer_is_logged(self):
        self.serve(PRODUCT)
        with patch("sys.stdout",new_callable=io.StringIO) as output:
            self.assertFalse(activate_coupons(self.page,"amazon"))
        self.assertIn("Verificando cupons",output.getvalue())
        self.assertIn("Nenhum cupom clicável",output.getvalue())
