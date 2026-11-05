"""Compila, verifica fora do projeto e empacota o CompraHub para Windows x64."""
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"


def run(command, **kwargs):
    print("Executando:", subprocess.list2cmdline([str(part) for part in command]), flush=True)
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def collect_licenses():
    target = ROOT / "build" / "third_party_licenses"
    target.mkdir(parents=True, exist_ok=True)
    for package in ("playwright", "greenlet", "pyee", "typing_extensions", "pyinstaller"):
        distribution = metadata.distribution(package)
        copied = 0
        for relative in distribution.files or []:
            name = str(relative).replace("\\", "/")
            if any(word in Path(name).name.lower() for word in ("license", "copying", "notice")):
                original = Path(distribution.locate_file(relative))
                if original.is_file():
                    destination = target / package / name.replace("..", "_").lstrip("/")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, destination)
                    copied += 1
        if not copied:
            raise RuntimeError(f"Avisos de licença não encontrados para {package}.")
    python_license = next((
        Path(sys.base_prefix) / name for name in ("LICENSE.txt", "LICENSE")
        if (Path(sys.base_prefix) / name).is_file()
    ), None)
    if not python_license:
        raise RuntimeError("Licença da distribuição Python não encontrada.")
    (target / "python").mkdir(exist_ok=True)
    shutil.copyfile(python_license, target / "python" / "LICENSE.txt")
    return target


def verify_bundle_inputs():
    import playwright
    registry = json.loads((Path(playwright.__file__).parent / "driver" / "package" / "browsers.json").read_text())
    chromium = next(item for item in registry["browsers"] if item["name"] == "chromium")
    browser = ROOT / ".browsers" / f"chromium-{chromium['revision']}" / "chrome-win64" / "chrome.exe"
    if not browser.is_file():
        raise RuntimeError("Instale o Chromium local com PLAYWRIGHT_BROWSERS_PATH=.browsers.")
    if sys.platform != "win32" or sys.maxsize <= 2**32:
        raise RuntimeError("Compile em Windows com Python de 64 bits.")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_executable(executable):
    # Somente o .exe é copiado; sem código-fonte, Python ou navegador externos.
    with tempfile.TemporaryDirectory(prefix="CompraHub-portatil-") as folder:
        isolated = Path(folder) / "pasta com espaço e acentuação"
        isolated.mkdir()
        copied = isolated / "CompraHub.exe"
        shutil.copyfile(executable, copied)
        report = isolated / "verification.json"
        env = dict(os.environ)
        env["COMPRAHUB_DATA_DIR"] = str(isolated / "dados-isolados")
        for key in ("PYTHONHOME", "PYTHONPATH", "PLAYWRIGHT_BROWSERS_PATH", "PLAYWRIGHT_NODEJS_PATH"):
            env.pop(key, None)
        system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        env["PATH"] = str(system / "System32") + os.pathsep + str(system)
        run([copied, "--self-test", "--report", report], cwd=isolated, env=env)
        data = json.loads(report.read_text(encoding="utf-8"))
        if not data.get("ok") or not data.get("frozen"):
            raise RuntimeError("O diagnóstico do executável isolado não passou.")
        shutil.copyfile(report, DIST / "VERIFICACAO.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Gerar executável e ZIP do CompraHub.")
    parser.add_argument("--skip-tests", action="store_true", help="Usar apenas quando a suíte já passou para este código.")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    verify_bundle_inputs()
    if not args.skip_tests:
        run([sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"])
    licenses = collect_licenses()
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", ROOT / "CompraHub.spec"])
    executable = DIST / "CompraHub.exe"
    verify_executable(executable)
    archive = DIST / "CompraHub-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        package.write(executable, "CompraHub.exe")
        for name in ("LEIA-ME.txt", "THIRD_PARTY_NOTICES.md"):
            package.write(ROOT / "distribution" / name, name)
        for item in sorted(licenses.rglob("*")):
            if item.is_file():
                package.write(item, str(Path("third_party_licenses") / item.relative_to(licenses)))
    for name in ("LEIA-ME.txt", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / "distribution" / name, DIST / name)
    checksums = "".join(f"{sha256(path)}  {path.name}\n" for path in (executable, archive))
    (DIST / "SHA256SUMS.txt").write_text(checksums, encoding="ascii")
    print(f"Pronto: {executable} ({executable.stat().st_size / 1024**2:.1f} MiB)", flush=True)
    print(f"ZIP: {archive}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
