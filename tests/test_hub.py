import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hub import HubServer, JobManager
from hub_store import Conflict, ProductStore, normalize_url, price_cents
from runtime import BotStopped, check_stop, emit_status, wait

URL = "https://www.amazon.com.br/dp/B0HK2KVYD7"
OTHER_URL = "https://www.amazon.com.br/gp/product/B000000001"


def product(url=URL, name="Meu console", quantity=2, max_price="4.500,00"):
    return {"url": url, "name": name, "quantity": quantity, "max_price": max_price, "interval_seconds": 120}


class FakeProcess:
    """Processo simulado: não abre Amazon nem executa compras."""
    instances = []

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.code = None
        self.stop_file = Path(kwargs["env"]["AMAZON_BOT_STOP_FILE"])
        self.event_file = Path(kwargs["env"]["AMAZON_BOT_EVENT_FILE"])
        FakeProcess.instances.append(self)

    def poll(self):
        if self.stop_file.exists() and self.code is None:
            self.code = 0
        return self.code

    def wait(self, timeout=None):
        code = self.poll()
        if code is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return code

    def terminate(self):
        self.code = -15


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "products.json"
        self.store = ProductStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_normalizes_supported_urls_and_discards_tracking(self):
        for value in (
            URL + "?tag=affiliate&ref=abc",
            "amazon.com.br/dp/b0hk2kvyd7/ref=test",
            "https://amazon.com.br/gp/product/B0HK2KVYD7",
            "https://www.amazon.com.br/gp/aw/d/B0HK2KVYD7",
        ):
            self.assertEqual(normalize_url(value), ("B0HK2KVYD7", URL))

    def test_rejects_foreign_hosts_credentials_ports_and_short_links(self):
        for value in (
            "https://amazon.com.br.example.com/dp/B0HK2KVYD7",
            "https://example.com/dp/B0HK2KVYD7",
            "https://www.amazon.com.br@evil.com/dp/B0HK2KVYD7",
            "https://user:password@www.amazon.com.br/dp/B0HK2KVYD7",
            "http://www.amazon.com.br/dp/B0HK2KVYD7",
            "https://www.amazon.com.br:9999/dp/B0HK2KVYD7",
            "https://amzn.to/example", "https://www.amazon.com.br/s?k=console", None,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_url(value)

    def test_prices_are_converted_to_exact_cents(self):
        for text, cents in [("4.500,00", 450000), ("4500", 450000), ("4500.01", 450001), ("R$ 0,01", 1)]:
            self.assertEqual(price_cents(text), cents)
        for text in ("-1", "0", "1.234", "1,234", "NaN", "1e3", True, "R$ 4.500,000"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                price_cents(text)

    def test_create_edit_reload_and_delete(self):
        created = self.store.save(product())
        self.assertEqual(created["quantity"], 2)
        edited = self.store.save(product(quantity=3, max_price="4400,01"), created["id"])
        self.assertEqual(edited["max_unit_cents"], 440001)
        loaded = ProductStore(self.path)
        self.assertEqual(loaded.get(created["id"]), edited)
        loaded.remove(created["id"])
        self.assertEqual(ProductStore(self.path).list(), [])

    def test_duplicate_asin_cannot_create_another_record(self):
        self.store.save(product())
        with self.assertRaises(Conflict):
            self.store.save(product(URL + "?tag=other"))
        self.assertEqual(len(self.store.list()), 1)

    def test_bad_fields_do_not_change_saved_data(self):
        existing = self.store.save(product())
        for data in [product(quantity=0), product(quantity=1.5), product(name="x"*101),
                     {**product(), "interval_seconds":1}]:
            with self.assertRaises(ValueError):
                self.store.save(data, existing["id"])
        self.assertEqual(self.store.get(existing["id"]), existing)

    def test_failed_write_rolls_back_memory(self):
        original = self.store.save(product())
        with patch.object(self.store, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.save(product(quantity=4), original["id"])
        self.assertEqual(self.store.get(original["id"]), original)

    def test_corrupt_file_is_not_silently_replaced(self):
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProductStore(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "broken")


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = ProductStore(self.path / "products.json")
        self.first = self.store.save(product())
        self.second = self.store.save(product(OTHER_URL, name="Outro produto"))
        self.manager = JobManager(self.store, self.path, popen=FakeProcess)

    def tearDown(self):
        self.manager.shutdown()
        self.temp.cleanup()

    def test_start_passes_quantity_and_generated_configuration_without_shell(self):
        self.manager.start(self.first["id"])
        fake = self.manager.jobs[self.first["id"]]["process"]
        self.assertFalse(fake.kwargs["shell"])
        self.assertEqual(fake.command[-2:], ["--quantidade", "2"])
        config_path = Path(fake.command[fake.command.index("--config")+1])
        config = json.loads(config_path.read_text())
        self.assertEqual(config, {"asin":"B0HK2KVYD7", "max_unit_cents":450000, "interval_seconds":120})
        self.assertTrue(self.manager.snapshot()["products"][0]["active"])

    def test_only_one_product_runs_and_active_settings_are_locked(self):
        self.manager.start(self.first["id"])
        with self.assertRaises(Conflict):
            self.manager.start(self.second["id"])
        with self.assertRaises(Conflict):
            self.manager.save_product(product(quantity=3), self.first["id"])
        with self.assertRaises(Conflict):
            self.manager.remove_product(self.first["id"])

    def test_stop_keeps_product_and_does_not_restart_worker(self):
        self.manager.start(self.first["id"])
        fake = self.manager.jobs[self.first["id"]]["process"]
        self.manager.stop(self.first["id"])
        self.assertTrue(fake.stop_file.exists())
        snapshot = self.manager.snapshot()
        self.assertIsNone(snapshot["active_id"])
        self.assertEqual(snapshot["products"][0]["status"]["state"], "stopped")
        self.assertEqual(len(self.store.list()), 2)

    def test_ready_and_confirmed_are_taken_from_worker_events(self):
        self.manager.start(self.first["id"])
        fake = self.manager.jobs[self.first["id"]]["process"]
        fake.event_file.write_text(json.dumps({"state":"ready","title":"Pronto","detail":"Total validado"}))
        self.assertEqual(self.manager.snapshot()["products"][0]["status"]["state"], "ready")
        fake.event_file.write_text(json.dumps({"state":"confirmed","title":"Confirmado","detail":"Amazon"}))
        fake.code = 0
        for _ in range(2):
            self.assertEqual(self.manager.snapshot()["products"][0]["status"]["state"], "confirmed")

    def test_old_ready_event_cannot_override_stopped_or_finished(self):
        self.manager.start(self.first["id"])
        fake = self.manager.jobs[self.first["id"]]["process"]
        fake.event_file.write_text(json.dumps({"state":"ready","title":"Pronto","detail":""}))
        self.manager.snapshot()
        self.manager.stop(self.first["id"])
        for _ in range(2):
            self.assertEqual(self.manager.snapshot()["products"][0]["status"]["state"], "stopped")
        self.manager.start(self.second["id"])
        fake = self.manager.jobs[self.second["id"]]["process"]
        fake.event_file.write_text(json.dumps({"state":"ready","title":"Pronto","detail":""}))
        fake.code = 0
        for _ in range(2):
            self.assertEqual(self.manager.snapshot()["products"][1]["status"]["state"], "finished")

    def test_failed_worker_has_error_and_activity(self):
        self.manager.start(self.first["id"])
        job = self.manager.jobs[self.first["id"]]
        job["log_handle"].write("Falha simulada\n")
        job["log_handle"].flush()
        job["process"].code = 1
        self.assertEqual(self.manager.snapshot()["products"][0]["status"]["state"], "error")
        self.assertIn("Falha simulada", self.manager.logs(self.first["id"]))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProductStore(Path(self.temp.name) / "products.json")
        self.manager = JobManager(self.store, self.temp.name, popen=FakeProcess)
        self.server = HubServer(("127.0.0.1", 0), self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.shutdown()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, method="GET", data=None, token=True, extra=None):
        headers = {"X-Hub-Token": self.server.token} if token else {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra or {})
        request = Request(self.server.origin + path, method=method, headers=headers,
                          data=json.dumps(data).encode() if data is not None else None)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, raw

    def test_page_and_state_require_expected_local_access(self):
        code, body = self.request("/", token=False)
        self.assertEqual(code, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertEqual(self.request("/api/state", token=False)[0], 403)
        self.assertEqual(self.request("/api/state")[0], 200)
        self.assertEqual(self.request("/api/state", extra={"Host":"evil.example"})[0], 403)

    def test_foreign_origin_cannot_start_or_modify_products(self):
        code, body = self.request("/api/products", "POST", product(), extra={"Origin":"https://evil.example"})
        self.assertEqual(code, 403)
        self.assertEqual(self.store.list(), [])

    def test_crud_start_and_stop_api(self):
        code, body = self.request("/api/products", "POST", product())
        self.assertEqual(code, 200)
        saved = json.loads(body)["products"][0]
        path = "/api/products/" + saved["id"]
        self.assertEqual(self.request(path, "PUT", product(quantity=3))[0], 200)
        self.assertEqual(self.request(path + "/start", "POST")[0], 200)
        self.assertEqual(self.request(path, "DELETE")[0], 409)
        self.assertEqual(self.request(path + "/stop", "POST")[0], 200)
        self.assertEqual(self.request(path, "DELETE")[0], 200)
        self.assertEqual(self.store.list(), [])

    def test_bad_links_are_rejected_and_unknown_resources_return_404(self):
        self.assertEqual(self.request("/api/products", "POST", product("https://127.0.0.1/product"))[0], 400)
        self.assertEqual(self.request("/api/products/" + "f"*32, "DELETE")[0], 404)


class RuntimeTests(unittest.TestCase):
    def test_stop_before_browser_launch_exits_cleanly(self):
        import bot
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"asin":"B0HK2KVYD7","max_unit_cents":450000,"interval_seconds":120}))
            stop = Path(folder) / "stop"
            stop.touch()
            with patch.dict(os.environ, {"AMAZON_BOT_STOP_FILE":str(stop)}), \
                 patch("playwright.sync_api.sync_playwright") as browser, \
                 patch("sys.stdout", new_callable=io.StringIO):
                code = bot.main(["--config", str(config), "--quantidade", "2"])
                self.assertEqual(code, 0)
                browser.assert_not_called()

    def test_stop_interrupts_long_wait_without_buying(self):
        with tempfile.TemporaryDirectory() as folder:
            stop_file = Path(folder) / "stop"
            with patch.dict(os.environ, {"AMAZON_BOT_STOP_FILE":str(stop_file)}):
                page = unittest.mock.Mock()
                def stop(milliseconds):
                    stop_file.touch()
                page.wait_for_timeout.side_effect = stop
                with self.assertRaises(BotStopped):
                    wait(page, 120000)
                self.assertEqual(page.wait_for_timeout.call_count, 1)

    def test_status_is_written_without_login_or_card_data(self):
        with tempfile.TemporaryDirectory() as folder:
            event = Path(folder) / "event.json"
            with patch.dict(os.environ, {"AMAZON_BOT_EVENT_FILE":str(event)}):
                emit_status("Pronto para sua confirmação", "Total validado", "success")
            saved = json.loads(event.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "ready")
            self.assertEqual(saved["detail"], "Total validado")
            self.assertNotIn("password", saved)


if __name__ == "__main__":
    unittest.main()
