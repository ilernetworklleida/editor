"""
setup.py — Wizard interactivo para crear .env y verificar dependencias.

Uso:
    python scripts/setup.py

Comprueba:
  - Python version
  - FFmpeg + ffprobe instalados
  - yt-dlp instalado
  - .venv presente
  - Crea .env desde .env.example si no existe
  - Genera EDITOR_SECRET aleatorio
  - Pide ADMIN_EMAIL / ADMIN_PASS interactivamente
  - Pide opcionalmente ANTHROPIC_API_KEY
  - Verifica acceso a la API de Claude si la diste
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


def info(msg: str) -> None:
    print(f"[..] {msg}")


def ok(msg: str) -> None:
    print(f"[OK] {msg}")


def err(msg: str) -> None:
    print(f"[!!] {msg}")


def check_command(name: str) -> tuple[bool, str]:
    """Comprueba si un comando esta en el PATH y devuelve su version."""
    path = shutil.which(name)
    if not path:
        return False, ""
    try:
        out = subprocess.run(
            [name, "--version"],
            capture_output=True, text=True, timeout=8,
        )
        first_line = (out.stdout or out.stderr).strip().split("\n")[0]
        return True, first_line
    except Exception:
        return True, "(version desconocida)"


def check_python_module(mod: str) -> bool:
    """Comprueba si un modulo Python esta instalado."""
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def ask(prompt: str, default: str = "", required: bool = False,
        secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    if required and not default:
        suffix = " (REQUERIDO)"
    full = f"  {prompt}{suffix}: "
    while True:
        if secret:
            try:
                import getpass
                v = getpass.getpass(full).strip()
            except Exception:
                v = input(full).strip()
        else:
            v = input(full).strip()
        if not v:
            v = default
        if required and not v:
            err("Este valor es requerido. Intenta de nuevo.")
            continue
        return v


def main() -> None:
    print()
    print("====================================")
    print("  REEL/LAB - Setup wizard")
    print("====================================")
    print()

    # 1. Python
    info(f"Python {sys.version.split()[0]}")
    if sys.version_info < (3, 10):
        err("Python 3.10+ recomendado.")

    # 2. FFmpeg + ffprobe + yt-dlp
    print()
    info("Comprobando dependencias del sistema...")
    for cmd in ["ffmpeg", "ffprobe", "yt-dlp"]:
        present, ver = check_command(cmd)
        if present:
            ok(f"{cmd:10s} {ver}")
        else:
            err(f"{cmd:10s} NO encontrado en PATH")
            if cmd in ("ffmpeg", "ffprobe"):
                print("              Instala con: winget install Gyan.FFmpeg")
            else:
                print("              Instala con: winget install yt-dlp.yt-dlp")

    # 3. Python deps
    print()
    info("Comprobando paquetes Python...")
    deps = {
        "faster_whisper": "transcripcion (REQUERIDO)",
        "fastapi": "web UI (REQUERIDO)",
        "uvicorn": "servidor web (REQUERIDO)",
        "anthropic": "Claude API smart highlights (opcional)",
        "apscheduler": "scheduler de jobs (opcional)",
    }
    missing_required = []
    for mod, desc in deps.items():
        if check_python_module(mod):
            ok(f"{mod:18s} - {desc}")
        else:
            err(f"{mod:18s} - {desc}")
            if "REQUERIDO" in desc:
                missing_required.append(mod)
    if missing_required:
        print()
        err(f"Faltan paquetes obligatorios: {', '.join(missing_required)}")
        print("Ejecuta: pip install -r requirements.txt")
        sys.exit(1)

    # 4. .env
    print()
    if ENV_FILE.exists():
        info(f".env ya existe en {ENV_FILE}")
        ans = ask("Sobrescribir? (y/N)", default="N")
        if ans.lower() != "y":
            ok("Setup completado (sin cambios en .env)")
            print()
            print("Lanza el server con:")
            print("  python scripts/run_web.py")
            return
    else:
        info(".env no existe, vamos a crearlo")

    # 5. Pide credenciales
    print()
    print("Configura el admin (login del web UI):")
    admin_email = ask("Email admin", default="info@ilernetworklleida.com")
    admin_pass = ask("Password admin (deja vacio = sin auth, modo local)",
                     secret=True)

    # 6. Pide API key opcional
    print()
    print("Configura Claude API (opcional, para smart highlights):")
    print("  Si no tienes, dejalo vacio. Saca tu key en")
    print("  https://console.anthropic.com/")
    api_key = ask("ANTHROPIC_API_KEY", secret=True)

    if api_key:
        info("Verificando acceso a Claude API...")
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key, timeout=10.0)
            client.models.retrieve("claude-haiku-4-5")
            ok("API key valida")
        except Exception as e:
            err(f"No se pudo verificar la key: {e}")
            ans = ask("Guardar igualmente? (y/N)", default="N")
            if ans.lower() != "y":
                api_key = ""

    # 7. Webhook opcional
    print()
    webhook = ask("Webhook URL (Slack/Discord/IFTTT, opcional)", default="")

    # 8. Genera EDITOR_SECRET
    secret_key = secrets.token_hex(32)

    # 9. Escribe .env
    print()
    info(f"Escribiendo {ENV_FILE}...")
    lines = [
        "# Generado por scripts/setup.py",
        "",
        f"ADMIN_EMAIL={admin_email}",
    ]
    if admin_pass:
        lines.append(f"ADMIN_PASS={admin_pass}")
    else:
        lines.append("# ADMIN_PASS=  (sin auth, modo local)")
    lines.append(f"EDITOR_SECRET={secret_key}")
    lines.append("")
    if api_key:
        lines.append(f"ANTHROPIC_API_KEY={api_key}")
        lines.append("HIGHLIGHTS_MODEL=claude-opus-4-7")
    else:
        lines.append("# ANTHROPIC_API_KEY=  (define para activar smart highlights con IA)")
    if webhook:
        lines.append("")
        lines.append(f"WEBHOOK_URL={webhook}")
    lines.append("")
    ENV_FILE.write_text("\n".join(lines), encoding="utf-8")
    ok(f".env escrito ({len(lines)} lineas)")

    # 10. Final
    print()
    print("====================================")
    print("  Setup completo")
    print("====================================")
    print()
    print("Lanza el server:")
    print("  python scripts/run_web.py")
    print()
    print("Despues abre http://localhost:8000")
    if admin_pass:
        print(f"Login: {admin_email} + tu password")
    else:
        print("(sin auth, modo local — define ADMIN_PASS en .env si lo expones)")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Cancelado.")
        sys.exit(130)
