"""Entrada única do executável: hub, processo do bot ou diagnóstico offline."""
import multiprocessing
from pathlib import Path
import sys
import traceback

from app_paths import VERSION, browser_environment, data_root, is_frozen


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    browser_environment()
    if args and args[0] == "--worker":
        from bot import main as bot_main
        return bot_main(args[1:])
    if args and args[0] == "--self-test":
        from package_diagnostics import main as diagnostics_main
        return diagnostics_main(args[1:])
    if args == ["--version"]:
        print(f"CompraHub {VERSION}")
        return 0
    from hub import main as hub_main
    return hub_main(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except Exception:
        details = traceback.format_exc()
        print(details, file=sys.stderr)
        if is_frozen() and not any(flag in sys.argv for flag in ("--worker", "--self-test")):
            root = data_root()
            root.mkdir(parents=True, exist_ok=True)
            report = root / "erro-inicializacao.log"
            report.write_text(details, encoding="utf-8")
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None, f"Não foi possível iniciar o CompraHub.\nDetalhes em:\n{report}",
                    "CompraHub", 0x10,
                )
            except Exception:
                pass
        raise SystemExit(1)
