"""
telegram_bot.py — Bot de Telegram que lanza jobs desde el chat.

Usa el endpoint /api/run del web app local. Solo deps stdlib + requests
(que ya viene con anthropic).

Configuracion via env (en .env o exportadas):
    TELEGRAM_BOT_TOKEN=123456:ABC...        (de @BotFather)
    TELEGRAM_ALLOWED_USERS=123456789,987... (tus user IDs Telegram)
    EDITOR_API_TOKEN=tok_...                (de /tokens en REEL/LAB)
    EDITOR_BASE_URL=http://localhost:8000   (default)
    EDITOR_PUBLIC_URL=https://reels.tudom.com (para link clickable, opcional)

Comandos en Telegram:
    /start            intro
    /help             ayuda
    /reels <URL> [N]  crea job con N reels (default 6)
    /status           ultimo job creado por el bot
    /jobs             lista los ultimos 5 jobs

Uso:
    python scripts/telegram_bot.py

(Lanzalo en otra terminal alongside run_web.py)
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(p: Path) -> None:
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USERS = {
    s.strip() for s in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if s.strip()
}
API_TOKEN = os.environ.get("EDITOR_API_TOKEN", "").strip()
BASE_URL = os.environ.get("EDITOR_BASE_URL", "http://localhost:8000").rstrip("/")
PUBLIC_URL = os.environ.get("EDITOR_PUBLIC_URL", BASE_URL).rstrip("/")

if not BOT_TOKEN:
    print("[ERROR] TELEGRAM_BOT_TOKEN no definido")
    sys.exit(1)
if not API_TOKEN:
    print("[!!] EDITOR_API_TOKEN no definido — el bot solo funcionara si "
          "REEL/LAB tampoco tiene auth (modo local)")
if not ALLOWED_USERS:
    print("[!!] TELEGRAM_ALLOWED_USERS vacio — cualquiera con tu @bot podra "
          "lanzar jobs. Pon tu user_id (envia /start a @userinfobot para "
          "averiguarlo)")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
LAST_JOB: dict[str, str] = {}  # chat_id -> last job_id


def tg_send(chat_id: int | str, text: str, parse_mode: str = "Markdown") -> None:
    try:
        httpx.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                  "disable_web_page_preview": False},
            timeout=10.0,
        )
    except Exception as e:
        print(f"[tg] sendMessage fallo: {e}")


def is_authorized(user_id: int | str) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS


def call_api_run(url: str, n_clips: int = 6,
                 profile: str = "viral",
                 ai_highlights: bool = True,
                 instructions: str = "") -> dict | None:
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["X-API-Token"] = API_TOKEN
    payload = {
        "url": url,
        "n_clips": n_clips,
        "profile": profile,
        "ai_highlights": ai_highlights,
    }
    if instructions:
        payload["instructions"] = instructions
    try:
        r = httpx.post(
            f"{BASE_URL}/api/run",
            json=payload,
            headers=headers,
            timeout=15.0,
        )
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def call_api_job(job_id: str) -> dict | None:
    headers = {"X-API-Token": API_TOKEN} if API_TOKEN else {}
    try:
        r = httpx.get(f"{BASE_URL}/api/job/{job_id}", headers=headers, timeout=8.0)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


HELP_TEXT = """
*REEL/LAB Bot*

Comandos:
`/reels <URL> [N]` - crea job de N reels (default 6)
`/status` - estado del ultimo job que lanzaste
`/jobs` - lista de tus ultimos 5 jobs
`/help` - este mensaje

Ejemplos:
`/reels https://www.youtube.com/watch?v=ABC123`
`/reels https://www.tiktok.com/@user/video/12345 4`
"""


def handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "").strip()

    if not is_authorized(user_id):
        tg_send(chat_id, f"No autorizado (user_id `{user_id}`). "
                          f"Anade tu ID a TELEGRAM_ALLOWED_USERS en .env.")
        return

    if text in ("/start", "/help"):
        tg_send(chat_id, HELP_TEXT)
        return

    if text.startswith("/reels"):
        parts = text.split()
        if len(parts) < 2:
            tg_send(chat_id, "Uso: `/reels <URL> [N]`")
            return
        url = parts[1]
        n_clips = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 6
        if not url.startswith("http"):
            tg_send(chat_id, "URL invalida")
            return
        tg_send(chat_id, f"⏳ Creando job para {n_clips} reels...")
        res = call_api_run(url, n_clips=n_clips)
        if not res or "error" in res:
            tg_send(chat_id, f"❌ Error: {res.get('error','?') if res else 'sin respuesta'}")
            return
        job_id = res.get("job_id", "?")
        LAST_JOB[str(chat_id)] = job_id
        link = f"{PUBLIC_URL}/job/{job_id}"
        tg_send(chat_id, f"✅ Job creado: `{job_id}`\n[Ver progreso]({link})\n\n"
                         f"Te avisare cuando termine (o usa `/status`).")
        return

    if text == "/status":
        job_id = LAST_JOB.get(str(chat_id))
        if not job_id:
            tg_send(chat_id, "No has lanzado ningun job aun. Usa `/reels <URL>`.")
            return
        data = call_api_job(job_id)
        if not data:
            tg_send(chat_id, f"No pude leer el job `{job_id}`.")
            return
        status = data.get("status", "?")
        prog = data.get("progress", {})
        label = prog.get("label", status)
        pct = prog.get("percent", 0)
        link = f"{PUBLIC_URL}/job/{job_id}"
        tg_send(chat_id, f"`{job_id}`: *{status}* ({pct}% — {label})\n"
                          f"[Abrir]({link})")
        return

    if text == "/jobs":
        # Llamada al jobs page no esta en /api, pero podemos saltar.
        tg_send(chat_id, f"Lista completa: {PUBLIC_URL}/jobs")
        return

    # Si pega solo una URL, asume /reels
    if text.startswith("http"):
        tg_send(chat_id, f"Lanzando con defaults (6 reels, perfil viral)...")
        res = call_api_run(text)
        if res and "job_id" in res:
            LAST_JOB[str(chat_id)] = res["job_id"]
            link = f"{PUBLIC_URL}/job/{res['job_id']}"
            tg_send(chat_id, f"✅ Job `{res['job_id']}` en cola\n[Progreso]({link})")
        else:
            tg_send(chat_id, f"❌ Error: {res.get('error', '?') if res else 'sin respuesta'}")
        return

    tg_send(chat_id, "No te entiendo. Usa `/help`.")


def main() -> None:
    print(f"[bot] REEL/LAB Telegram bot conectando a {BASE_URL}")
    print(f"[bot] {len(ALLOWED_USERS)} users autorizados" if ALLOWED_USERS else "[bot] AUTH ABIERTA (cuidado)")

    offset = 0
    while True:
        try:
            r = httpx.get(
                f"{TG_API}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40.0,
            )
            data = r.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if msg and "text" in msg:
                    try:
                        handle_message(msg)
                    except Exception as e:
                        print(f"[bot] error en handle_message: {e}")
        except KeyboardInterrupt:
            print("\n[bot] detenido")
            break
        except Exception as e:
            print(f"[bot] error en poll: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
