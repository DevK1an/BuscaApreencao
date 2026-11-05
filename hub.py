"""Hub HTTP local para gerenciar produtos e uma execução do bot por vez."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
import webbrowser

from hub_store import Conflict, ProductStore
from app_paths import RESOURCE_ROOT, data_root, is_frozen, worker_command
from price_history import PriceHistory

ROOT = RESOURCE_ROOT
WEB_ROOT = ROOT / "hub_web"
STATES = {"running", "monitoring", "needs_attention", "preparing", "ready", "confirmed", "error", "stopped", "finished", "opened"}
PRODUCT_ROUTE = re.compile(r"/api/products/([a-f0-9]{32})(?:/(start|stop|logs|prices))?$")


class JobManager:
    def __init__(self, store, data_dir, root=ROOT, popen=None):
        self.store = store
        self.data_dir = Path(data_dir)
        self.root = Path(root)
        self.popen = popen or subprocess.Popen
        self.lock = threading.RLock()
        self.jobs = {}
        self.active_id = None
        self.history = PriceHistory(self.data_dir / "prices.sqlite3")

    def _refresh(self):
        for product_id, job in self.jobs.items():
            if not job["active"]:
                continue
            process = job["process"]
            try:
                event = json.loads(job["event_file"].read_text(encoding="utf-8"))
                if event.get("state") in STATES:
                    job["event"] = event
            except (OSError, ValueError, AttributeError):
                pass
            code = process.poll()
            if code is not None and job["active"]:
                job["active"] = False
                if self.active_id == product_id:
                    self.active_id = None
                if job["stopping"]:
                    job["event"] = {"state": "stopped", "title": "Monitoramento parado", "detail": "Você pode iniciar outro produto."}
                elif code != 0:
                    job["event"] = {
                        "state": "error", "title": "O bot foi encerrado com erro",
                        "detail": "Veja a atividade. Feche outras execuções que estejam usando o navegador do bot.",
                    }
                elif job["event"].get("state") not in ("confirmed", "error"):
                    job["event"] = {"state": "finished", "title": "Execução encerrada", "detail": "A janela do navegador foi fechada."}
                job["log_handle"].close()
            if job["stopping"] and job["active"]:
                job["event"] = {"state": "stopping", "title": "Parando monitoramento", "detail": "Aguardando o navegador concluir a operação atual."}

    def snapshot(self):
        with self.lock:
            self._refresh()
            result = []
            for product in self.store.list():
                job = self.jobs.get(product["id"])
                event = dict(job["event"]) if job else {
                    "state": "idle", "title": "Pronto para iniciar", "detail": "Escolha Iniciar para monitorar este produto."
                }
                result.append({**product, "active": bool(job and job["active"]), "status": event})
            return {"products": result, "active_id": self.active_id, "max_active": 1}

    def save_product(self, data, product_id=None):
        with self.lock:
            self._refresh()
            if product_id and self.active_id == product_id:
                raise Conflict("Pare o monitoramento antes de editar este produto.")
            old = self.store.get(product_id) if product_id else None
            saved = self.store.save(data,product_id)
            if old and old["asin"] != saved["asin"]:
                self.history.clear(product_id)
            return saved

    def remove_product(self, product_id):
        with self.lock:
            self._refresh()
            if self.active_id == product_id:
                raise Conflict("Pare o monitoramento antes de excluir este produto.")
            self.store.remove(product_id)
            self.history.clear(product_id)
            self.jobs.pop(product_id, None)

    def start(self, product_id):
        with self.lock:
            self._refresh()
            if self.active_id:
                raise Conflict("Já existe um produto em execução. Pare o atual antes de iniciar outro.")
            product = self.store.get(product_id)
            job_dir = self.data_dir / "runs" / uuid.uuid4().hex
            job_dir.mkdir(parents=True, exist_ok=False)
            config_file = job_dir / "config.json"
            config = {
                "asin":product["asin"], "max_unit_cents":product["max_unit_cents"],
                "interval_seconds":product["interval_seconds"],
            }
            if product.get("mode") == "search":
                config.update({
                    "mode":"search","query":product["query"],"marketplaces":product["marketplaces"],
                    "pages_per_store":product["pages_per_store"],"allow_partial":product.get("allow_partial",True),
                    "search_provider":product.get("search_provider","google_shopping"),
                    "excluded_stores":product.get("excluded_stores",["AliExpress","Shopee"]),
                })
            elif product.get("marketplace") in ("mercadolivre","external"):
                config.update({"marketplace":product["marketplace"],"url":product["url"]})
            config_file.write_text(json.dumps(config), encoding="utf-8")
            event_file = job_dir / "event.json"
            stop_file = job_dir / "stop"
            log_file = job_dir / "activity.log"
            log_handle = log_file.open("w", encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "AMAZON_BOT_EVENT_FILE": str(event_file),
                "AMAZON_BOT_STOP_FILE": str(stop_file),
                "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
                "COMPRAHUB_HISTORY_FILE":str(self.history.path),
                "COMPRAHUB_PRODUCT_ID":product_id,
                "COMPRAHUB_PRODUCT_NAME":product["name"],
            })
            try:
                process = self.popen(
                    worker_command(config_file, product["quantity"], self.root),
                    cwd=str(self.root), env=env, stdin=subprocess.DEVNULL,
                    stdout=log_handle, stderr=subprocess.STDOUT, shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                log_handle.close()
                raise
            self.jobs[product_id] = {
                "process": process, "event_file": event_file, "stop_file": stop_file,
                "log_file": log_file, "log_handle": log_handle,
                "active": True, "stopping": False,
                "event": {
                    "state": "running", "title": "Iniciando o monitoramento",
                    "detail": "O bot vai usar a quantidade e o teto configurados neste produto.",
                },
            }
            self.active_id = product_id

    def stop(self, product_id):
        with self.lock:
            self._refresh()
            self.store.get(product_id)
            job = self.jobs.get(product_id)
            if job and job["active"]:
                job["stop_file"].touch()
                job["stopping"] = True

    def logs(self, product_id):
        with self.lock:
            self.store.get(product_id)
            job = self.jobs.get(product_id)
            if not job:
                return ""
            try:
                with job["log_file"].open("rb") as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    stream.seek(max(0, size - 24000))
                    return stream.read().decode("utf-8", errors="replace")
            except OSError:
                return ""

    def shutdown(self):
        with self.lock:
            for job in self.jobs.values():
                if job["active"]:
                    job["stop_file"].touch()
            for job in self.jobs.values():
                if job["active"]:
                    try:
                        job["process"].wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        job["process"].terminate()
                        try:
                            job["process"].wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            pass
                job["log_handle"].close()


class HubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager, token=None):
        self.manager = manager
        self.token = token or secrets.token_urlsafe(32)
        super().__init__(address, HubHandler)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class HubHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _check_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _authorized(self):
        token = self.headers.get("X-Hub-Token", "")
        if not hmac.compare_digest(token, self.server.token):
            return False
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        return origin is None or origin in (self.server.origin, f"http://localhost:{port}")

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Envie dados em JSON.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Tamanho da requisição inválido.") from None
        if not 0 < length <= 65536:
            raise ValueError("Requisição inválida ou muito grande.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        if not self._check_host():
            return self._send(403, {"error": "Acesso permitido somente pelo endereço local do hub."})
        path = urlsplit(self.path).path
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/styles.css": ("styles.css", "text/css; charset=utf-8"), "/tracking.js":("tracking.js","text/javascript; charset=utf-8")}
        if path in assets:
            name, mime = assets[path]
            content = (WEB_ROOT / name).read_bytes()
            if name == "index.html":
                content = content.replace(b"__HUB_TOKEN__", self.server.token.encode("ascii"))
            return self._send(200, content, mime)
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if not self._authorized():
            return self._send(403, {"error": "Reabra o hub para atualizar sua sessão local."})
        if path == "/api/state":
            return self._send(200, self.server.manager.snapshot())
        match = PRODUCT_ROUTE.fullmatch(path)
        if match and match[2] == "prices":
            try:
                product=self.server.manager.store.get(match[1])
                query=product.get("query") if product.get("mode")=="search" else None
                return self._send(200,self.server.manager.history.get(match[1],query=query))
            except KeyError:
                return self._send(404,{"error":"Produto não encontrado."})
        if match and match[2] == "logs":
            try:
                return self._send(200, {"logs": self.server.manager.logs(match[1])})
            except KeyError:
                return self._send(404, {"error": "Produto não encontrado."})
        self._send(404, {"error": "Página não encontrada."})

    def _mutate(self, method):
        if not self._check_host() or not self._authorized():
            return self._send(403, {"error": "Requisição não autorizada. Reabra o hub."})
        path = urlsplit(self.path).path
        match = PRODUCT_ROUTE.fullmatch(path)
        manager = self.server.manager
        try:
            if method == "POST" and path == "/api/products":
                manager.save_product(self._body())
            elif match and method == "PUT" and match[2] is None:
                manager.save_product(self._body(), match[1])
            elif match and method == "DELETE" and match[2] is None:
                manager.remove_product(match[1])
            elif match and method == "POST" and match[2] == "start":
                manager.start(match[1])
            elif match and method == "POST" and match[2] == "stop":
                manager.stop(match[1])
            else:
                return self._send(404, {"error": "Operação não encontrada."})
            self._send(200, manager.snapshot())
        except Conflict as error:
            self._send(409, {"error": str(error)})
        except KeyError:
            self._send(404, {"error": "Produto não encontrado."})
        except (ValueError, UnicodeError) as error:
            self._send(400, {"error": str(error)})
        except OSError:
            self._send(500, {"error": "Não foi possível salvar ou iniciar o bot. Confira as permissões da pasta."})

    def do_POST(self):
        self._mutate("POST")

    def do_PUT(self):
        self._mutate("PUT")

    def do_DELETE(self):
        self._mutate("DELETE")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hub local de produtos Amazon.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Não abrir o painel automaticamente.")
    args = parser.parse_args(argv)
    data_dir = data_root() / ".hub"
    seed = None if is_frozen() else ROOT / "config.json"
    store = ProductStore(data_dir / "products.json", seed)
    manager = JobManager(store, data_dir)
    try:
        server = HubServer(("127.0.0.1", args.port), manager)
    except OSError:
        print(f"A porta {args.port} já está em uso. Se o hub já estiver aberto, acesse http://127.0.0.1:{args.port}")
        return 1
    print(f"Hub disponível em {server.origin}", flush=True)
    print("Mantenha este terminal aberto. Ctrl+C encerra o hub e o monitoramento.", flush=True)
    if not args.no_open:
        webbrowser.open(server.origin)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nEncerrando o hub...", flush=True)
    finally:
        server.server_close()
        manager.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
