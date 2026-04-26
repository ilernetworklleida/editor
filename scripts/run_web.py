"""
run_web.py — Lanza el servidor web del Editor (FastAPI + uvicorn).

Uso:
    python scripts/run_web.py                          # localhost:8000
    python scripts/run_web.py --host 0.0.0.0           # accesible en LAN
    python scripts/run_web.py --port 8080
    python scripts/run_web.py --host 0.0.0.0 --port 8080

Una vez arrancado: abre http://localhost:8000 en el navegador.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
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
