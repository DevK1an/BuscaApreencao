"""Estado e parada cooperativa do bot iniciado pelo hub local."""
import json
import os
from pathlib import Path
import threading
import time
from atomic_files import replace_file

_lock = threading.Lock()
_previous = None


class BotStopped(Exception):
    pass


def check_stop():
    path = os.environ.get("AMAZON_BOT_STOP_FILE")
    if path and Path(path).exists():
        raise BotStopped()


def emit_status(title, detail="", tone="info", state=None):
    global _previous
    target = os.environ.get("AMAZON_BOT_EVENT_FILE")
    if not target:
        return
    if state is None:
        state = {
            "Monitorando o produto": "monitoring",
            "Rastreando preços":"monitoring",
            "Comparação incompleta":"needs_attention",
            "Aguardando acesso à loja":"needs_attention",
            "Aguardando acesso à Amazon": "needs_attention",
            "Preparando seu pedido": "preparing",
            "Aplicando cupom": "preparing",
            "Conferindo inclusão no carrinho": "preparing",
            "Recusando proteção adicional": "preparing",
            "Produto aberto na loja": "opened",
            "Aguardando acesso ao Google": "needs_attention",
            "Pedido ainda não validado": "needs_attention",
            "Pronto para sua confirmação": "ready",
            "Confirmação exibida pela Amazon": "confirmed",
            "Preparação interrompida": "error",
        }.get(title, "running")
    signature = (target, title, detail, tone, state)
    with _lock:
        if signature == _previous:
            return
        path = Path(target)
        payload = {"title": title, "detail": detail, "tone": tone, "state": state, "updated_at": time.time()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            replace_file(temporary,path)
            _previous = signature
        except OSError:
            # O monitor pode continuar se apenas a atualização do hub falhar.
            pass


def wait(page, milliseconds):
    remaining = milliseconds
    while remaining > 0:
        check_stop()
        step = min(remaining, 1000)
        page.wait_for_timeout(step)
        remaining -= step
    check_stop()
