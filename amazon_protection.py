"""Recusa somente ofertas opcionais de seguro/proteção da Amazon."""
import re
from urllib.parse import urlsplit

from policy import CannotVerify

PROTECTION_ROOTS = (
    "#attach-warranty-pane, #attach-desktop-sideSheet, #attach-protection-plan-pane, "
    "#attach-insurance-pane, [data-testid='protection-plan-dialog'], "
    "[role='dialog'], dialog, .a-popover"
)
PROTECTION = re.compile(
    r"seguro|garantia\s+estendida|plano\s+de\s+prote[cç][aã]o|"
    r"cobrir\s+este\s+produto|protection\s+plan|extended\s+warranty", re.I,
)
DECLINE = re.compile(
    r"^(?:n[aã]o,?\s+obrigad[oa]|n[aã]o\s+adicionar(?:\s+(?:seguro|prote[cç][aã]o|garantia))?|"
    r"continuar\s+sem\s+(?:seguro|prote[cç][aã]o|garantia(?:\s+estendida)?)|"
    r"n[aã]o\s+quero\s+(?:seguro|prote[cç][aã]o)|no,?\s+thanks|"
    r"decline(?:\s+(?:coverage|protection))?)[.!]?$", re.I,
)


def trusted_amazon_frame(frame):
    parsed = urlsplit(frame.url)
    if parsed.scheme == "https" and parsed.hostname in {"www.amazon.com.br", "amazon.com.br"}:
        return True
    return (
        frame.url in ("about:blank", "about:srcdoc")
        and frame.parent_frame is not None
        and trusted_amazon_frame(frame.parent_frame)
    )


def _forbidden_button(node):
    return node.evaluate("""el => el.name === 'placeYourOrder1'
        || ['submitOrderButtonId','placeYourOrder'].includes(el.id)
        || Boolean(el.closest('#placeYourOrder, #attachSiAddCoverage, #attach-insurance-add-button'))""")


def decline_protection_offers(page, attempted):
    """Clica uma vez na recusa explícita, só dentro de uma oferta de proteção."""
    from playwright.sync_api import Error
    from runtime import check_stop
    from status import show_status

    check_stop()
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.hostname != "www.amazon.com.br":
        raise CannotVerify("A recusa de seguro saiu da Amazon Brasil.")
    pages = [page]
    for other in page.context.pages:
        if other is not page and not other.is_closed() and other.opener() is page:
            pages.append(other)

    for tab in pages:
        for frame in tab.frames:
            if not trusted_amazon_frame(frame):
                continue
            for root in frame.locator(PROTECTION_ROOTS).all():
                if not root.is_visible():
                    continue
                text = root.inner_text()
                if not PROTECTION.search(text):
                    continue
                known = root.get_attribute("id") in {
                    "attach-warranty-pane", "attach-desktop-sideSheet",
                    "attach-protection-plan-pane", "attach-insurance-pane",
                }
                if not known and not re.search(r"adicionar|incluir|cobrir|contratar|proteger|add\s+(?:a\s+)?(?:plan|protection)", text, re.I):
                    continue
                # Usa o próprio controle acessível ou o texto exato da recusa (span da Amazon).
                candidates = root.locator("#attachSiNoCoverage, #attach-warranty-decline-button").all()
                candidates += root.get_by_role("button", name=DECLINE).all()
                candidates += root.get_by_text(re.compile(r"n[aã]o|no,?\s+thanks|continuar\s+sem|decline", re.I)).all()
                for node in candidates:
                    if not node.is_visible() or not node.is_enabled() or _forbidden_button(node):
                        continue
                    label = node.get_attribute("aria-label") or node.get_attribute("value") or node.inner_text()
                    label = re.sub(r"\s+", " ", label).strip()
                    if not DECLINE.fullmatch(label):
                        continue
                    # O mesmo modal pode aparecer em vários seletores ou trocar de DOM.
                    key = (tab, frame, frame.url)
                    if key in attempted:
                        continue
                    check_stop()
                    attempted.add(key)
                    show_status(page, "Recusando proteção adicional", "Continuando com o produto, sem adicionar seguro ou garantia estendida.")
                    try:
                        node.click(timeout=3000)
                    except Error as error:
                        raise CannotVerify(
                            "Não foi possível confirmar a recusa do seguro. Selecione Não, obrigado na janela da Amazon; o bot não repetirá o clique."
                        ) from error
                    print("[Seguro] Selecionado Não, obrigado. Conferindo a inclusão do produto sem proteção adicional.", flush=True)
                    return True
    return False
