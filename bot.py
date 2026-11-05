"""Abre o produto, prepara o checkout e aguarda confirmação pessoal na Amazon."""
import argparse
import json
import os
from datetime import datetime
from pathlib import Path
import sys

from amazon import (
    CART_URL, add_to_cart, advance_checkout, cart_items, checkout_review,
    is_order_confirmation, needs_human, open_checkout, product_price,
    require_exact_cart, set_quantity, wait_for_cart_add,
)
from policy import CannotVerify, OfferUnavailable, Settings, brl, check_review, positive_int
from status import show_status
from runtime import BotStopped, check_stop, emit_status, wait
from app_paths import RESOURCE_ROOT, browser_environment, data_root
from price_history import record_direct
from coupons import CouponApplier, activate_coupons, has_available_coupon

ROOT = RESOURCE_ROOT
browser_environment()


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def prompt_quantity():
    while True:
        try:
            return positive_int(input("Quantas unidades deseja? "), "Quantidade")
        except ValueError as error:
            print(error)


def notify():
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except (ImportError, RuntimeError):
        print("\a", end="", flush=True)


def wait_for_login(page):
    """Aguarda a interação na Amazon sem pedir Enter no terminal."""
    message = "Faça login ou resolva o CAPTCHA nesta janela. O bot continua automaticamente."
    log(message)
    while not page.is_closed() and needs_human(page):
        check_stop()
        show_status(page, "Aguardando acesso à Amazon", message, "warning")
        wait(page, 1000)


def wait_next_check(page, settings, message):
    log(message)
    show_status(
        page, "Monitorando o produto",
        f"{message}\nNova consulta em {settings.interval_seconds} segundos.",
    )
    wait(page, settings.interval_seconds * 1000)


def final_review(page, settings, quantity):
    """Avança apenas etapas conhecidas; lê continuamente a revisão sem finalizar."""
    log("Preparando o checkout. A confirmação final será feita por você na Amazon.")
    attempted = set()
    last_state = None
    approved = None
    coupons = CouponApplier("amazon")
    coupon_changed = False
    stable_after_coupon = None
    while not page.is_closed():
        check_stop()
        if is_order_confirmation(page):
            if last_state != "order-confirmed":
                log("A Amazon exibiu a página de confirmação. Confira os detalhes do pedido na sua conta.")
                notify()
                last_state = "order-confirmed"
            show_status(
                page, "Confirmação exibida pela Amazon",
                "Confira os detalhes do pedido. Feche esta janela quando terminar.", "success",
            )
            wait(page, 1000)
            continue
        if needs_human(page):
            wait_for_login(page)
            continue
        try:
            if coupons.apply(page):
                approved = None
                coupon_changed = True
                stable_after_coupon = None
                wait(page, 1000)
                continue
            review = checkout_review(page)
            check_review(review, settings, quantity)
            fingerprint = (review.asin, review.quantity, review.total_cents)
            if (coupon_changed or coupons.attempted) and fingerprint != stable_after_coupon:
                stable_after_coupon = fingerprint
                approved = None
                show_status(page, "Aplicando cupom", "Conferindo o total atualizado com frete antes de liberar sua confirmação.")
                wait(page, 1000)
                continue
        except CannotVerify as error:
            approved = None
            stable_after_coupon = None
            # Estes botões só avançam endereço/pagamento. O botão final nunca é acionado.
            if advance_checkout(page, attempted):
                show_status(page, "Preparando seu pedido", "Avançando com as opções salvas na Amazon.")
                wait(page, 1000)
                continue
            message = str(error)
            if message != last_state:
                log(f"NÃO VALIDADO: {message}")
                log("Se a Amazon pedir endereço, entrega ou pagamento, selecione nesta janela.")
                last_state = message
            show_status(
                page, "Pedido ainda não validado",
                f"{message}\nComplete as informações solicitadas pela Amazon, se houver.",
                "warning",
            )
        else:
            fingerprint = (review.asin, review.quantity, review.total_cents)
            detail = (
                f"{quantity} unidade(s). Total com frete: {brl(review.total_cents)}.\n"
                f"Teto do pedido: {brl(settings.max_unit_cents * quantity)}.\n"
                "Revise os dados e clique você mesmo na confirmação da Amazon."
            )
            if coupons.message:
                detail += "\n" + coupons.message
            if fingerprint != approved:
                notify()
                page.bring_to_front()
                log(f"PRONTO PARA SUA CONFIRMAÇÃO: {detail}")
                approved = fingerprint
            last_state = fingerprint
            show_status(page, "Pronto para sua confirmação", detail, "success")
        # Após aplicar cupons reconhecidos, somente lê o total; nunca confirma a compra.
        wait(page, 1000)


def run(settings, quantity):
    from playwright.sync_api import Error, sync_playwright

    check_stop()
    emit_status("Abrindo o produto na Amazon", settings.product_url)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(data_root() / ".amazon-profile"),
            headless=False,
            locale="pt-BR",
        )
        context.set_default_timeout(12_000)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_navigation_timeout(45_000)
        try:
            log(
                f"Abrindo direto {settings.product_url}. {quantity} unidade(s); "
                f"limite com frete: {brl(settings.max_unit_cents)} por unidade."
            )
            reuse_cart = False
            while not page.is_closed():
                try:
                    check_stop()
                    page.goto(settings.product_url, wait_until="domcontentloaded")
                    if needs_human(page):
                        wait_for_login(page)
                        continue
                    page.locator("#productTitle, #captchacharacters, #ap_email").first.wait_for(
                        state="attached", timeout=15_000
                    )
                    price = product_price(page, settings.asin)
                    record_direct(settings,quantity,price)
                    if price > settings.max_unit_cents and not has_available_coupon(page, "amazon"):
                        wait_next_check(
                            page, settings,
                            f"Preço anunciado: {brl(price)} por unidade, acima do limite.",
                        )
                        continue
                    if price > settings.max_unit_cents:
                        log("Cupom disponível: o pedido será conferido no checkout; nenhum desconto será presumido para aceitar o teto.")
                    set_quantity(page, quantity)
                    # Confere o carrinho somente quando há uma oferta candidata.
                    cart_page = context.new_page()
                    try:
                        cart_page.goto(CART_URL, wait_until="domcontentloaded")
                        if needs_human(cart_page):
                            cart_page.bring_to_front()
                            wait_for_login(cart_page)
                            cart_page.goto(CART_URL, wait_until="domcontentloaded")
                        existing = cart_items(cart_page)
                        if existing and existing != [(settings.asin, quantity)]:
                            raise CannotVerify(
                                "Há outros itens ou outra quantidade no carrinho. "
                                "Ajuste o carrinho na Amazon; a consulta será repetida."
                            )
                        reuse_cart = bool(existing)
                    finally:
                        cart_page.close()
                    # Selecionar quantidade pode atualizar a oferta; releia o preço.
                    if product_price(page, settings.asin) <= settings.max_unit_cents or has_available_coupon(page, "amazon"):
                        break
                    wait_next_check(page, settings, "A oferta mudou de preço; aguardando o limite.")
                except OfferUnavailable as error:
                    wait_next_check(page, settings, str(error))
                except (CannotVerify, Error) as error:
                    if page.is_closed():
                        return
                    if needs_human(page):
                        wait_for_login(page)
                        continue
                    wait_next_check(
                        page, settings,
                        f"Consulta sem oferta confirmada: {str(error).splitlines()[0]}",
                    )
            if page.is_closed():
                return

            # Após esta etapa, nenhuma falha retorna ao monitor ou repete uma adição.
            check_stop()
            if activate_coupons(page, "amazon"):
                if product_price(page, settings.asin) > settings.max_unit_cents and not has_available_coupon(page, "amazon"):
                    raise CannotVerify("A oferta mudou de preço durante a ativação do cupom. Confira a Amazon.")
                set_quantity(page, quantity)
            emit_status("Preparando seu pedido", "Conferindo o carrinho antes de abrir o checkout.", state="preparing")
            if reuse_cart:
                log("O produto já está no carrinho na quantidade escolhida. Reutilizando sem duplicar.")
            else:
                log("Oferta encontrada. Adicionando a quantidade escolhida uma única vez.")
                add_to_cart(page)
                wait_for_cart_add(page, settings.asin, quantity)
            page.goto(CART_URL, wait_until="domcontentloaded")
            if needs_human(page):
                wait_for_login(page)
                page.goto(CART_URL, wait_until="domcontentloaded")
            require_exact_cart(page, settings.asin, quantity)
            activate_coupons(page, "amazon")
            require_exact_cart(page, settings.asin, quantity)
            check_stop()
            open_checkout(page)
            page.wait_for_load_state("domcontentloaded")
            final_review(page, settings, quantity)
        except BotStopped:
            log("Monitoramento parado pelo hub.")
            emit_status("Monitoramento parado", state="stopped")
        except (CannotVerify, Error) as error:
            if not page.is_closed():
                message = str(error).splitlines()[0]
                log(f"Fluxo interrompido: {message}")
                log("Nenhuma nova adição será tentada. Confira a Amazon ou feche a janela para encerrar.")
                while not page.is_closed():
                    try:
                        show_status(page, "Preparação interrompida", message, "warning")
                        wait(page, 1000)
                    except (BotStopped, Error):
                        break
        except KeyboardInterrupt:
            log("Encerrado por você.")
        finally:
            context.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monitor Amazon com confirmação pessoal no navegador.")
    parser.add_argument("--quantidade", type=int, help="Unidades (1 a 999); pergunta se omitido.")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.config.read_text(encoding="utf-8-sig"))
        quantity = prompt_quantity() if args.quantidade is None else positive_int(args.quantidade,"Quantidade")
        if raw.get("mode") == "search":
            from price_tracker import SearchSettings, run as run_search
            run_search(SearchSettings.load(raw), quantity)
        elif raw.get("marketplace") in ("mercadolivre","external") or str(raw.get("asin","")).startswith("MLB"):
            from price_tracker import run_ml_link
            # Valida limites e URL novamente antes de abrir a loja.
            from hub_store import validate_product
            checked = validate_product({
                **raw,"quantity":quantity,
                "max_price":f"{raw['max_unit_cents']//100}.{raw['max_unit_cents']%100:02d}",
            })
            run_ml_link(checked, quantity)
        else:
            settings = Settings.load(args.config)
            run(settings, quantity)
    except BotStopped:
        log("Monitoramento parado pelo hub.")
        emit_status("Monitoramento parado", state="stopped")
    except (ValueError, OSError, KeyError) as error:
        print(f"Configuração inválida: {error}", file=sys.stderr)
        return 1
    except ImportError:
        print("Execute instalar.cmd antes de iniciar o bot.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nEncerrado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
