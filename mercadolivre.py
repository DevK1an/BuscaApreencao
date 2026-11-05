"""Preparação conservadora no Mercado Livre; confirmação final sempre manual."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from amazon import visible_first
from hub_store import normalize_url
from market_search import parse_amount
from policy import CannotVerify, OfferUnavailable, Review, check_review, brl
from runtime import check_stop, wait
from status import show_status
from coupons import CouponApplier, activate_coupons, has_available_coupon

HOSTS = {"www.mercadolivre.com.br","mercadolivre.com.br","produto.mercadolivre.com.br"}
CART_URL = "https://www.mercadolivre.com.br/gz/cart/v2"


@dataclass(frozen=True)
class MLSettings:
    asin: str
    max_unit_cents: int
    interval_seconds: int
    product_url: str


def require_ml(page):
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS:
        raise CannotVerify("Resolva o acesso e retorne ao Mercado Livre.")


def needs_human(page):
    host = urlsplit(page.url).hostname or ""
    path = urlsplit(page.url).path
    return host.startswith("auth.") or "/login" in path or "/registration" in path or page.locator(
        'input[name="user_id"], input[name="password"], #captcha'
    ).count() > 0


def product_price(page):
    require_ml(page)
    node = visible_first(page, ".ui-pdp-price__second-line .andes-money-amount, [data-testid='product-price']")
    if node is None:
        raise OfferUnavailable("Preço principal do Mercado Livre não reconhecido.")
    data = node.evaluate("""el => {
        const value=s=>el.querySelector(s)?.textContent || '';
        return {symbol:value('.andes-money-amount__currency-symbol'),
            whole:value('.andes-money-amount__fraction'),fraction:value('.andes-money-amount__cents')};
    }""")
    return parse_amount(data, "mercadolivre")


def set_quantity(page, quantity):
    node = visible_first(page, "select[name='quantity'], select#quantity")
    if node is not None:
        values = node.locator("option").evaluate_all("els=>els.filter(e=>!e.disabled).map(e=>e.value)")
        if str(quantity) not in values:
            raise OfferUnavailable("Quantidade solicitada indisponível no Mercado Livre.")
        node.select_option(str(quantity))
        return
    control = visible_first(page, ".ui-pdp-buybox__quantity, [data-testid='quantity-selector']")
    if control is None:
        if quantity != 1:
            raise CannotVerify("Selecione a quantidade no Mercado Livre; o seletor não foi reconhecido.")
        return
    selected = re.search(r"(\d+)\s*unidade", control.inner_text(), re.I)
    if selected and int(selected[1]) == quantity:
        return
    control.click()
    option = page.get_by_role("option", name=re.compile(rf"^{quantity}\s+unidades?$", re.I))
    if option.count() != 1:
        raise CannotVerify("A opção da quantidade desejada não foi reconhecida.")
    option.click()
    if not re.search(rf"\b{quantity}\s*unidade", control.inner_text(), re.I):
        raise CannotVerify("A quantidade não foi confirmada no Mercado Livre.")


def cart_items(page):
    require_ml(page)
    items = []
    roots = page.locator("[data-testid='cart-item'], .ui-cart-item, .cart-item")
    for root in roots.all():
        if not root.is_visible():
            continue
        keys = set()
        for link in root.locator("a[href]").all():
            try:
                key, _ = normalize_url(link.get_attribute("href") or "")
                if key.startswith("MLB"):
                    keys.add(key)
            except ValueError:
                pass
        raw = root.get_attribute("data-quantity")
        select = root.locator("select[name='quantity'], input[name='quantity']")
        if select.count():
            raw = select.first.input_value()
        if not raw:
            match = re.search(r"(?:quantidade|qtd)\s*:?\s*(\d+)", root.inner_text(), re.I)
            raw = match[1] if match else None
        if len(keys) != 1 or not raw or not raw.isdigit():
            raise CannotVerify("Não foi possível conferir o produto e a quantidade no carrinho do Mercado Livre.")
        items.append((next(iter(keys)), int(raw)))
    if not items:
        body = page.locator("body").inner_text().lower()
        if not any(text in body for text in ("seu carrinho está vazio","seu carrinho esta vazio","carrinho vazio")):
            raise CannotVerify("O layout do carrinho do Mercado Livre não foi reconhecido.")
    return items


def checkout_review(page, settings, quantity):
    require_ml(page)
    final = page.get_by_role("button", name=re.compile(r"^(Confirmar compra|Confirmar e pagar|Pagar)$", re.I))
    if not any(button.is_visible() for button in final.all()):
        raise CannotVerify("Complete os dados no Mercado Livre até a revisão final do pedido.")
    total = visible_first(page, "[data-testid='order-total'], .summary__total .andes-money-amount")
    if total is None:
        raise CannotVerify("O total final do Mercado Livre ainda não foi reconhecido.")
    if total.locator(".andes-money-amount__fraction").count():
        data = total.evaluate("""el=>({
            symbol:el.querySelector('.andes-money-amount__currency-symbol')?.textContent,
            whole:el.querySelector('.andes-money-amount__fraction')?.textContent,
            fraction:el.querySelector('.andes-money-amount__cents')?.textContent
        })""")
        cents = parse_amount(data, "mercadolivre")
    else:
        from policy import parse_brl
        cents = parse_brl(total.inner_text())
    root = visible_first(page, "[data-testid='checkout-items'], .checkout-items")
    if root is None:
        raise CannotVerify("Os itens da revisão do Mercado Livre não foram reconhecidos.")
    keys = set()
    for link in root.locator("a[href]").all():
        try:
            key, _ = normalize_url(link.get_attribute("href") or "")
            keys.add(key)
        except ValueError:
            pass
    labels = re.findall(r"(?:quantidade|qtd)\s*:?\s*(\d+)", root.inner_text(), re.I)
    if keys != {settings.asin} or len(labels) != 1 or int(labels[0]) != quantity:
        raise CannotVerify("Produto ou quantidade da revisão do Mercado Livre divergem do solicitado.")
    review = Review(settings.asin, quantity, cents)
    check_review(review, settings, quantity)
    return review


def prepare(page, context, settings, quantity):
    from bot import notify, log
    require_ml(page)
    set_quantity(page, quantity)
    if activate_coupons(page, "mercadolivre"):
        if product_price(page) > settings.max_unit_cents and not has_available_coupon(page, "mercadolivre"):
            raise CannotVerify("A oferta mudou de preço durante a ativação do cupom. Confira o Mercado Livre.")
        set_quantity(page, quantity)
    check_stop()
    cart_page = context.new_page()
    try:
        cart_page.goto(CART_URL, wait_until="domcontentloaded")
        existing = cart_items(cart_page)
        if existing and existing != [(settings.asin, quantity)]:
            raise CannotVerify("Ajuste os itens do carrinho do Mercado Livre antes de preparar esta oferta.")
    finally:
        cart_page.close()
    if not existing:
        add = page.get_by_role("button", name=re.compile(r"^Adicionar ao carrinho$", re.I))
        if add.count() != 1:
            raise CannotVerify("Adicionar ao carrinho não foi reconhecido no Mercado Livre.")
        check_stop()
        add.click()
    page.goto(CART_URL, wait_until="domcontentloaded")
    if cart_items(page) != [(settings.asin, quantity)]:
        raise CannotVerify("O carrinho do Mercado Livre não contém exatamente a quantidade solicitada.")
    activate_coupons(page, "mercadolivre")
    if cart_items(page) != [(settings.asin, quantity)]:
        raise CannotVerify("O cupom alterou os itens do carrinho. Confira a quantidade na loja.")
    proceed = page.get_by_role("button", name=re.compile(r"^Continuar (a )?compra$", re.I))
    if proceed.count() != 1:
        raise CannotVerify("Abra a etapa de checkout na janela do Mercado Livre.")
    check_stop()
    proceed.click()
    previous = None
    coupons = CouponApplier("mercadolivre")
    coupon_changed = False
    stable_after_coupon = None
    while not page.is_closed():
        check_stop()
        try:
            if coupons.apply(page):
                coupon_changed = True
                stable_after_coupon = None
                previous = None
                wait(page, 1000)
                continue
            review = checkout_review(page, settings, quantity)
            if (coupon_changed or coupons.attempted) and review.total_cents != stable_after_coupon:
                stable_after_coupon = review.total_cents
                show_status(page, "Aplicando cupom", "Conferindo o total atualizado com frete antes de liberar sua confirmação.")
                wait(page, 1000)
                continue
            state = ("ready", review.total_cents)
            detail = f"{quantity} unidade(s), total com frete {brl(review.total_cents)}. Revise e confirme pessoalmente no Mercado Livre."
            if coupons.message:
                detail += " " + coupons.message
            if state != previous:
                notify()
                log("PRONTO PARA SUA CONFIRMAÇÃO NO MERCADO LIVRE: " + detail)
                page.bring_to_front()
            show_status(page, "Pronto para sua confirmação", detail, "success")
        except CannotVerify as error:
            stable_after_coupon = None
            state = ("waiting", str(error))
            show_status(page, "Pedido ainda não validado", str(error), "warning")
        previous = state
        wait(page, 1000)
