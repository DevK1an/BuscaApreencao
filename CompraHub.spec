# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import json
import playwright

root = Path(SPECPATH)
registry = json.loads((Path(playwright.__file__).parent / "driver" / "package" / "browsers.json").read_text())
chromium = next(item for item in registry["browsers"] if item["name"] == "chromium")
browser = root / ".browsers" / f"chromium-{chromium['revision']}"
if not (browser / "chrome-win64" / "chrome.exe").exists():
    raise SystemExit("Chromium não encontrado. Execute python -m playwright install chromium.")
licenses = root / "build" / "third_party_licenses"
if not licenses.is_dir():
    raise SystemExit("Use build_exe.py para preparar os avisos das dependências.")
# Lista explícita: nenhum cadastro, cookie, perfil ou log local entra na distribuição.
datas = [
    (str(root / "hub_web"), "hub_web"),
    (str(browser), f".browsers/{browser.name}"),
    (str(licenses), "third_party_licenses"),
    (str(root / "distribution" / "THIRD_PARTY_NOTICES.md"), "."),
]
for prefix in ("ffmpeg-", "winldd-"):
    for component in (root / ".browsers").glob(prefix + "*"):
        if component.is_dir():
            datas.append((str(component), f".browsers/{component.name}"))

a = Analysis(
    [str(root / "desktop.py")], pathex=[str(root)], binaries=[], datas=datas,
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tkinter", "unittest"], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="CompraHub", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True,
    version=str(root / "distribution" / "version_info.txt"),
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
