"""
run_web.py — Lanza el servidor web del Editor (FastAPI + uvicorn).

Uso:
    python scripts/run_web.py                          # localhost:8000
    python scripts/run_web.py --host 0.0.0.0           # accesible en LAN
    python scripts/run_web.py --port 8080
    python scripts/run_web.py --host 0.0.0.0 --port 8080

Una vez arrancado: abre http://localhost:8000 en el navegador.

Auth: si existe un .env junto al proyecto con EDITOR_USER/EDITOR_PASS,
se cargan automaticamente y el servidor exige HTTP Basic Auth.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Carga un archivo .env simple (KEY=VALUE por linea) en os.environ."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


def main() -> None:
    load_dotenv(ROOT / ".env")
    args = sys.argv[1:]
    host = "127.0.0.1"
    port = "8000"
    if "--host" in args:
        host = args[args.index("--host") + 1]
    if "--port" in args:
        port = args[args.index("--port") + 1]

    cmd = [
        sys.executable, "-m", "uvicorn",
        "app.main:app",
        "--host", host,
        "--port", str(port),
        "--reload",
    ]
    print(f"[..] Editor web en http://{host if host != '0.0.0.0' else 'localhost'}:{port}")
    print(f"[..] Ctrl+C para parar")
    try:
        subprocess.run(cmd, cwd=str(ROOT))
    except KeyboardInterrupt:
        print("\n[OK] Servidor detenido")


if __name__ == "__main__":
    main()
