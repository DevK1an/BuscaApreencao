"""Leitura da Amazon e avanço de etapas anteriores à confirmação final.

Nunca aciona Comprar agora, Finalizar pedido ou Confirmar pagamento.
Seletores desconhecidos interrompem o fluxo.
"""
import re
import unicodedata
from urllib.parse import urlsplit

from policy import CannotVerify, OfferUnavailable, Review, parse_brl

CART_URL = "https://www.amazon.com.br/gp/cart/view.html"
PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePrice_feature_div .priceToPay .a-offscreen",
    "#apex_desktop .priceToPay .a-offscreen",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
)
FINAL_BUTTONS = (
    "input[name='placeYourOrder1'], button[name='placeYourOrder1'], "
    "#submitOrderButtonId, #placeYourOrder input[type='submit'], #placeYourOrder button"
)
SUMMARY_ROWS = (
    "#subtotals-marketplace-table tr, #subtotals tr, #order-summary tr, "
    "#orderSummary tr, [data-testid='order-summary'] tr"
)
ITEM_ROOTS = ("#spc-orders", "#checkout-item-block", "#order-items", "#shipments")
ASIN_LINK = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", re.I)


def normalized(text):
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(ch) != "Mn"
    )


def require_amazon(page):
    url = urlsplit(page.url)
    if url.scheme != "https" or url.hostname != "www.amazon.com.br":
        raise CannotVerify("A navegação saiu da Amazon Brasil.")


def visible_first(page, selector):
    for locator in page.locator(selector).all():
        if locator.is_visible():
            return locator
    return None


def needs_human(page):
    if "/ap/" in urlsplit(page.url).path:
        return True
    return visible_first(
        page, "#captchacharacters, input[name='cvf_captcha_input'], "
        "#ap_email, #ap_password, #auth-mfa-otpcode"
    ) is not None


PRICE_CONTAINER_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .priceToPay",
    "#corePriceDisplay_desktop_feature_div .apex-pricetopay-value",
    "#corePrice_feature_div .priceToPay",
    "#corePrice_feature_div .apex-pricetopay-value",
    "#apex_desktop .priceToPay",
    "#apex_desktop .apex-pricetopay-value",
)


def _offer_price(container):
    """Lê um único preço da oferta, incluindo o layout com a-offscreen vazio."""
    data = container.evaluate("""el => {
        const read = selector => Array.from(el.querySelectorAll(selector))
            .filter(n => n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden')
            .map(n => n.textContent || '');
        return {
            text: read('.a-offscreen'),
            symbol: read('.a-price-symbol'),
            whole: read('.a-price-whole'),
            fraction: read('.a-price-fraction')
        };
    }""")

    def clean(text):
        return re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", text).strip()

    prices = [parse_brl(clean(text)) for text in data["text"] if clean(text)]
    parts = (data["symbol"], data["whole"], data["fraction"])
    if any(parts):
        if not all(len(part) == 1 for part in parts):
            raise CannotVerify("Partes do preço principal ausentes ou ambíguas.")
        symbol, whole, fraction = [
            re.sub(r"\s+", "", clean(part[0])) for part in parts
        ]
        whole = whole.removesuffix(",")
        if (
            symbol != "R$"
            or not re.fullmatch(r"(?:[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+)", whole)
            or not re.fullmatch(r"[0-9]{2}", fraction)
        ):
            raise CannotVerify("Partes do preço principal em reais não reconhecidas.")
        prices.append(parse_brl(f"R$ {whole},{fraction}"))
    if not prices:
        return None
    if len(set(prices)) != 1 or prices[0] <= 0:
        raise CannotVerify("Preço principal ambíguo ou inválido.")
    return prices[0]


def product_price(page, asin):
    require_amazon(page)
    if needs_human(page):
        raise CannotVerify("A Amazon pediu login ou CAPTCHA. Resolva no navegador.")
    identifiers = page.locator("input#ASIN").all()
    if not identifiers or identifiers[0].input_value() != asin:
        raise CannotVerify("Não foi possível confirmar o código do produto.")
    add = visible_first(page, "#add-to-cart-button")
    if add is None or not add.is_enabled():
        raise OfferUnavailable("Produto sem opção de adicionar ao carrinho; aguardando estoque.")
    for selector in PRICE_CONTAINER_SELECTORS:
        prices = []
        for container in page.locator(selector).all():
            if container.is_visible():
                price = _offer_price(container)
                if price is not None:
                    prices.append(price)
        if prices:
            if len(set(prices)) != 1:
                raise CannotVerify("A oferta principal exibe preços divergentes.")
            return prices[0]
    # Layouts antigos apresentam um valor completo diretamente no buy box.
    for selector in ("#price_inside_buybox", "#newBuyBoxPrice"):
        prices = [
            parse_brl(node.text_content() or "")
            for node in page.locator(selector).all() if node.is_visible()
        ]
        if prices:
            if len(set(prices)) != 1 or prices[0] <= 0:
                raise CannotVerify("Preço principal ambíguo ou inválido.")
            return prices[0]
    raise CannotVerify("Preço principal ainda não reconhecido; nenhuma oferta foi escolhida.")


def set_quantity(page, quantity):
    select = page.locator("select#quantity")
    if select.count():
        options = select.locator("option").evaluate_all(
            "(nodes) => nodes.filter(n => !n.disabled).map(n => n.value)"
        )
        if str(quantity) not in options:
            raise OfferUnavailable(f"A oferta não permite selecionar {quantity} unidade(s).")
        select.select_option(str(quantity))
        if select.input_value() != str(quantity):
            raise CannotVerify("A seleção da quantidade não foi confirmada.")
    elif quantity != 1:
        raise OfferUnavailable("Seletor de quantidade indisponível para esta oferta.")


def cart_items(page):
    """Ignora itens salvos para depois e recomendações fora do carrinho ativo."""
    require_amazon(page)
    if needs_human(page):
        raise CannotVerify("Faça login e resolva os avisos da Amazon no navegador.")
    root = visible_first(page, "#sc-active-cart")
    if root is None:
        raise CannotVerify("Estrutura do carrinho não reconhecida.")
    items = []
    for item in root.locator("[data-asin][data-itemtype='active']").all():
        if not item.is_visible():
            continue
        asin = item.get_attribute("data-asin")
        if not asin:
            raise CannotVerify("Item do carrinho sem código de produto.")
        quantity_select = item.locator("select[name='quantity']")
        quantity_input = item.locator("input[name='quantity']")
        quantity_label = item.locator(".a-dropdown-prompt")
        raw = item.get_attribute("data-quantity")
        if quantity_select.count():
            raw = quantity_select.first.input_value()
        elif quantity_input.count():
            raw = quantity_input.first.input_value()
        elif quantity_label.count():
            raw = quantity_label.first.inner_text().strip()
        if not raw or not raw.isdigit() or int(raw) <= 0:
            raise CannotVerify("Quantidade no carrinho não reconhecida.")
        items.append((asin, int(raw)))
    # Um layout novo de itens não deve ser confundido com carrinho vazio.
    unknown = root.locator("[data-asin]:visible").all()
    known_asins = {asin for asin, _ in items}
    if any(node.get_attribute("data-asin") not in known_asins for node in unknown):
        raise CannotVerify("Há itens em uma estrutura de carrinho não reconhecida.")
    if not items:
        empty = visible_first(page, "#sc-empty-cart, .sc-your-amazon-cart-is-empty")
        text = normalized(root.inner_text())
        if empty is None and not any(t in text for t in ("carrinho esta vazio", "carrinho da amazon esta vazio")):
            raise CannotVerify("Não foi possível confirmar que o carrinho está vazio.")
    return items


def require_exact_cart(page, asin, quantity):
    actual = cart_items(page)
    if actual != [(asin, quantity)]:
        raise CannotVerify(
            f"O carrinho deve conter somente {asin} × {quantity}; encontrado: {describe_cart(actual)}. "
            "Ajuste-o manualmente; o bot não remove seus itens."
        )


def add_to_cart(page):
    require_amazon(page)
    button = visible_first(page, "#add-to-cart-button")
    if button is None or not button.is_enabled():
        raise CannotVerify("Adicionar ao carrinho não está disponível.")
    # Uma única tentativa. Falha depois deste ponto nunca gera uma nova adição.
    button.click()


def describe_cart(items):
    return ", ".join(f"{asin} × {quantity}" for asin, quantity in items) if items else "vazio"


def wait_for_cart_add(page, asin, quantity, timeout_ms=15000):
    """Confere o carrinho noutra aba, sem cancelar a inclusão assíncrona do produto."""
    import time
    from playwright.sync_api import Error
    from runtime import check_stop, wait
    from status import show_status
    from amazon_protection import decline_protection_offers

    expected = [(asin, quantity)]
    declined = set()
    deadline = time.monotonic() + timeout_ms / 1000
    last_detail = "O carrinho ainda está vazio."
    check_stop()
    print(f"[Carrinho] Inclusão solicitada uma vez: {describe_cart(expected)}. Aguardando confirmação da Amazon.", flush=True)
    show_status(page, "Conferindo inclusão no carrinho", "Aguardando a Amazon concluir a inclusão, sem repetir o clique.")
    probe = page.context.new_page()
    try:
        while time.monotonic() < deadline:
            check_stop()
            if page.is_closed():
                raise CannotVerify("A janela do produto foi fechada antes de confirmar a inclusão.")
            decline_protection_offers(page, declined)
            try:
                remaining = max(1, int((deadline-time.monotonic())*1000))
                response = probe.goto(CART_URL, wait_until="domcontentloaded", timeout=min(7000, remaining))
                if response and response.status >= 400:
                    raise CannotVerify(f"A consulta ao carrinho retornou HTTP {response.status}.")
                actual = cart_items(probe)
            except (CannotVerify, Error) as error:
                last_detail = str(error).splitlines()[0]
            else:
                if actual == expected:
                    if decline_protection_offers(page, declined):
                        wait(page, 250)
                        continue
                    print(f"[Carrinho] Confirmado pela Amazon: {describe_cart(actual)}.", flush=True)
                    return
                if actual:
                    raise CannotVerify(
                        f"Carrinho diferente do solicitado. Esperado: {describe_cart(expected)}; "
                        f"encontrado: {describe_cart(actual)}. Nenhum item foi removido ou adicionado novamente."
                    )
                last_detail = "O carrinho continua vazio."
            remaining = int((deadline-time.monotonic())*1000)
            if remaining > 0:
                wait(page, min(1000, remaining))
        raise CannotVerify(
            f"A Amazon não confirmou a inclusão de {describe_cart(expected)}. {last_detail} "
            "O botão Adicionar não será clicado novamente; confira o carrinho na loja."
        )
    finally:
        if not probe.is_closed():
            probe.close()


def open_checkout(page):
    require_amazon(page)
    button = visible_first(page, "input[name='proceedToRetailCheckout']")
    if button is None:
        raise CannotVerify("Botão de abrir o checkout não reconhecido. Abra-o manualmente.")
    button.click()


def checkout_review(page):
    require_amazon(page)
    if visible_first(page, FINAL_BUTTONS) is None:
        raise CannotVerify("Avance manualmente até a revisão final do pedido, sem confirmar ainda.")

    totals = []
    for row in page.locator(SUMMARY_ROWS).all():
        if not row.is_visible():
            continue
        text = row.inner_text().strip()
        norm = normalized(text)
        if re.match(r"^(total do pedido|total geral|total da compra|total a pagar)\s*:", norm):
            totals.append(parse_brl(text))
    if not totals or len(set(totals)) != 1:
        raise CannotVerify("Total final com frete não reconhecido; não foi considerado frete grátis.")

    candidates = []
    for selector in ITEM_ROOTS:
        root = visible_first(page, selector)
        if root is None:
            continue
        asins = set()
        for link in root.locator("a[href]").all():
            if not link.is_visible():
                continue
            match = ASIN_LINK.search(link.get_attribute("href") or "")
            if match:
                asins.add(match[1].upper())
        for node in root.locator("[data-asin]").all():
            if node.is_visible():
                asins.add((node.get_attribute("data-asin") or "").upper())
        # Cada rótulo precisa ser visível e explícito, sem usar quantidades de recomendações.
        labels = re.findall(r"(?:quantidade|qtd\.?)\s*:\s*(\d+)\b", normalized(root.inner_text()))
        if len(asins) == 1 and len(labels) == 1 and int(labels[0]) > 0:
            candidates.append((next(iter(asins)), int(labels[0])))
    if not candidates or len(set(candidates)) != 1:
        raise CannotVerify("Produto e quantidade da revisão final não reconhecidos.")
    asin, quantity = candidates[0]
    return Review(asin, quantity, totals[0])


# Botões que apenas avançam etapas anteriores à confirmação final.
CHECKOUT_CONTINUE_SELECTORS = (
    "#shipToThisAddressButton input",
    "input[name='shipToThisAddress']",
    "input[name='ppw-widgetEvent:SetPaymentPlanSelectContinueEvent']",
)


def advance_checkout(page, attempted):
    """Usa as opções já selecionadas; nunca escolhe nem confirma um pedido."""
    require_amazon(page)
    if needs_human(page) or visible_first(page, FINAL_BUTTONS) is not None:
        return False
    for selector in CHECKOUT_CONTINUE_SELECTORS:
        key = (page.url, selector)
        if key in attempted:
            continue
        button = visible_first(page, selector)
        if button is not None and button.is_enabled():
            # Não repete o clique se a Amazon permanecer na mesma etapa por validação.
            attempted.add(key)
            button.click()
            return True
    return False


def is_order_confirmation(page):
    parsed = urlsplit(page.url)
    return (
        parsed.scheme == "https" and parsed.hostname == "www.amazon.com.br"
        and parsed.path.startswith(("/gp/buy/thankyou/", "/checkout/thankyou"))
    )
