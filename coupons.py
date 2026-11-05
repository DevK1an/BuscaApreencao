"""Ativa cupons oferecidos pela própria loja, sem confirmar pedidos."""
import re
import time
from urllib.parse import urlsplit

from playwright.sync_api import Error
from policy import CannotVerify
from runtime import check_stop, wait
from status import show_status

ROOTS = {
    "amazon": (
        "#promoPriceBlockMessage_feature_div, #couponsInBuybox_feature_div, "
        "#coupon_feature_div, #couponFeature, #vpcButton, #spc-promotions, "
        "#spc-gcpromoinput, #sc-active-cart .sc-coupon, "
        "[data-testid='coupon-section'], [data-testid='coupon-card']"
    ),
    "mercadolivre": (
        ".ui-pdp-coupons, .ui-pdp-coupon, .ui-pdp-promotions, "
        ".coupon-section, .coupon-card, [data-testid='coupon-section'], "
        "[data-testid='coupon-card']"
    ),
}
HOSTS = {
    "amazon": {"www.amazon.com.br"},
    "mercadolivre": {"www.mercadolivre.com.br", "mercadolivre.com.br", "produto.mercadolivre.com.br"},
}
COUPON = re.compile(r"\b(?:cupom|cupons|coupon)\b", re.I)
ACTION = re.compile(
    r"^(?:aplicar|resgatar|coletar|destacar)(?:\s+(?:o|este))?\s+(?:cupom|coupon)"
    r"(?:\s+de\s+(?:R\$\s*[\d.,]+|[\d.,]+\s*%))?$", re.I,
)
SHORT_ACTION = re.compile(r"^(?:aplicar|resgatar|coletar|destacar)$", re.I)
APPLIED = re.compile(r"\b(?:cupom|coupon)\s+(?:foi\s+)?(?:aplicado|resgatado|destacado|ativado|selecionado|coletado)\b", re.I)
REJECTED = re.compile(r"\b(?:cupom|coupon)\s+(?:inválido|invalido|expirado|esgotado|indisponível|indisponivel|não aplicável|nao aplicavel)\b", re.I)


def _text(node):
    return re.sub(r"\s+", " ", node.inner_text()).strip()


def _checkbox_is_coupon(node, root):
    label = node.evaluate("""el => [
        el.id, el.name, el.getAttribute('aria-label'),
        ...[...(el.labels || [])].map(n => n.innerText),
        el.closest('label')?.innerText
    ].filter(Boolean).join(' ')""")
    if COUPON.search(label) or re.search(r"coupon|cupom|vpc|checkboxpctch", label, re.I):
        return True
    # Layout público da Amazon: label vazio e texto num span irmão do checkbox.
    return (
        root.locator(".couponLabelText").count() > 0
        and root.locator("input[type='checkbox'], [role='checkbox']").count() == 1
    )


def _is_coupon_button(button):
    if not button.is_visible() or not button.is_enabled():
        return False
    # A identidade do botão final prevalece mesmo se seu rótulo estiver incorreto.
    if button.evaluate("""el => el.name === 'placeYourOrder1'
        || ['submitOrderButtonId','placeYourOrder'].includes(el.id)
        || Boolean(el.closest('#placeYourOrder'))"""):
        return False
    label = button.get_attribute("aria-label") or button.get_attribute("value") or _text(button)
    label = re.sub(r"\s+", " ", label).strip()
    return bool(ACTION.fullmatch(label) or SHORT_ACTION.fullmatch(label))


class CouponApplier:
    def __init__(self, marketplace):
        if marketplace not in ROOTS:
            raise ValueError("Loja de cupom não reconhecida.")
        self.marketplace = marketplace
        self.attempted = set()
        self.pending = set()
        self.pending_started = None
        self.message = ""

    def _mark_pending(self, url):
        self.pending.add(url)
        if self.pending_started is None:
            self.pending_started = time.monotonic()

    def _clear_pending(self):
        self.pending.clear()
        self.pending_started = None

    def _pending_result(self):
        if self.pending_started is not None and time.monotonic()-self.pending_started < 8:
            return True
        raise CannotVerify("A loja não confirmou a ativação do cupom após a espera. Confira-o no navegador; o clique não será repetido.")

    def _roots(self, page):
        selector = ROOTS[self.marketplace]
        roots = [
            root for root in page.locator(selector).all()
            if root.is_visible()
            and not root.evaluate("(el, selector) => Boolean(el.parentElement?.closest(selector))", selector)
        ]
        dialogs = [
            root for root in page.locator("[role='dialog'], .andes-modal").all()
            if root.is_visible() and COUPON.search(_text(root))
        ]
        return dialogs + roots

    def _announce(self, page, message):
        self.message = message
        print("[Cupom] " + message, flush=True)
        show_status(page, "Aplicando cupom", message)

    def apply(self, page):
        """True pede nova conferência: uma ação foi feita ou a loja ainda está respondendo."""
        check_stop()
        parsed = urlsplit(page.url)
        if parsed.scheme != "https" or parsed.hostname not in HOSTS[self.marketplace]:
            raise CannotVerify("A aplicação de cupom saiu da loja esperada.")
        roots = self._roots(page)
        url = page.url
        for root in roots:
            text = _text(root)
            if REJECTED.search(text):
                self._clear_pending()
                self.message = "A loja informou que o cupom está indisponível. O teto será conferido sem estimar desconto."
                continue
            if APPLIED.search(text):
                self._clear_pending()
                self.message = "Cupom ativado pela loja. Confira o desconto no resumo do pedido."
                return False

        for root in roots:
            root_key = root.evaluate("el => el.id || el.getAttribute('data-testid') || el.getAttribute('role') || el.className")
            if REJECTED.search(_text(root)):
                continue
            for index, node in enumerate(root.locator("input[type='checkbox'], [role='checkbox']").all()):
                if not _checkbox_is_coupon(node, root):
                    continue
                if node.is_checked():
                    self._clear_pending()
                    self.message = "Cupom selecionado. O desconto efetivo será conferido no total da loja."
                    continue
                if not node.is_enabled():
                    continue
                key = (url, root_key, "checkbox", index)
                if key in self.attempted:
                    self._mark_pending(url)
                    continue
                target = node
                if not node.is_visible():
                    node_id = node.get_attribute("id")
                    target = next((label for label in root.locator("label").all()
                                   if node_id and label.get_attribute("for") == node_id and label.is_visible()), None)
                    if target is None:
                        continue
                check_stop()
                self.attempted.add(key)
                self._mark_pending(url)
                self._announce(page, "Selecionando o cupom disponível para este produto.")
                try:
                    if target is node:
                        node.check(timeout=3000)
                    else:
                        target.click(timeout=3000)
                    if node.is_checked():
                        self._clear_pending()
                except Error:
                    # Um clique pode ter sido aceito antes do timeout ou da troca do DOM.
                    # A próxima leitura confirma o resultado, sem clicar novamente.
                    pass
                return True

            if not COUPON.search(_text(root)):
                continue
            # Um campo vazio pede um código que não foi fornecido. Não inventa códigos.
            empty_code = any(
                node.is_visible() and not node.input_value().strip()
                for node in root.locator("input[type='text'], input:not([type]), textarea").all()
            )
            for index, button in enumerate(root.get_by_role("button").all()):
                if empty_code or not _is_coupon_button(button):
                    continue
                key = (url, root_key, "button", index)
                if key in self.attempted:
                    continue
                check_stop()
                self.attempted.add(key)
                self._mark_pending(url)
                self._announce(page, "Ativando o cupom oferecido pela loja; aguardando a confirmação do desconto.")
                try:
                    button.click(timeout=3000)
                except Error:
                    pass
                return True
        if self.pending:
            return self._pending_result()
        return False


def activate_coupons(page, marketplace):
    """Ativa cupons reconhecidos antes de adicionar/avançar; não estima descontos."""
    applier = CouponApplier(marketplace)
    print("[Cupom] Verificando cupons disponíveis na página atual.", flush=True)
    changed = False
    for _ in range(20):
        if not applier.apply(page):
            print("[Cupom] " + (applier.message or "Nenhum cupom clicável disponível nesta página."), flush=True)
            return changed
        changed = True
        wait(page, 750)
    raise CannotVerify("A ativação do cupom não foi confirmada; confira o cupom na janela da loja. O clique não será repetido.")


def has_available_coupon(page, marketplace):
    """Indício para consultar o checkout, nunca uma estimativa de desconto."""
    applier = CouponApplier(marketplace)
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS[marketplace]:
        return False
    for root in applier._roots(page):
        text = _text(root)
        if REJECTED.search(text):
            continue
        if APPLIED.search(text):
            return True
        for node in root.locator("input[type='checkbox'], [role='checkbox']").all():
            if _checkbox_is_coupon(node, root) and (node.is_checked() or node.is_enabled()):
                if node.is_visible() or root.locator(".couponLabelText").count():
                    return True
        if not COUPON.search(text):
            continue
        if any(node.is_visible() and not node.input_value().strip()
               for node in root.locator("input[type='text'], input:not([type]), textarea").all()):
            continue
        for button in root.get_by_role("button").all():
            if _is_coupon_button(button):
                return True
    return False
