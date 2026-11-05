"""Separa recursos distribuídos dos dados de cada computador."""
import os
from pathlib import Path
import sys

RESOURCE_ROOT = Path(__file__).resolve().parent
VERSION = "1.2.0"


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def data_root():
    override = os.environ.get("COMPRAHUB_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "CompraHub"
    return RESOURCE_ROOT


def browser_environment():
    path = str(RESOURCE_ROOT / ".browsers")
    if is_frozen():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path
    else:
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", path)


def worker_command(config_file, quantity, source_root=RESOURCE_ROOT):
    args = ["--config", str(config_file), "--quantidade", str(quantity)]
    if is_frozen():
        # sys.executable é o próprio CompraHub.exe, não um interpretador Python.
        return [sys.executable, "--worker", *args]
    return [sys.executable, "-X", "utf8", str(Path(source_root) / "bot.py"), *args]
