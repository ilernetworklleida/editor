"""
app/main.py — FastAPI web UI para auto_reels_pro.

Sirve una interfaz web completa para subir un video, configurar el pipeline
y ver los reels resultantes. Lanza con:

    python scripts/run_web.py

Auth: si defines EDITOR_USER y EDITOR_PASS (variables de entorno o .env),
todas las rutas requieren HTTP Basic Auth. Sin definirlas: modo local sin auth.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
JOBS_DIR = ROOT / "jobs"
PROFILES_DIR = ROOT / "profiles"
MUSIC_DIR = ROOT / "music"
BRANDING_DIR = ROOT / "branding"
SCRIPTS_DIR = ROOT / "scripts"
APP_DIR = ROOT / "app"
SCHEDULES_FILE = ROOT / "schedules.json"
API_TOKENS_FILE = ROOT / "api_tokens.json"
WATCH_DIR = ROOT / "watch"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}

for _d in [INPUT_DIR, OUTPUT_DIR, JOBS_DIR, PROFILES_DIR, MUSIC_DIR, BRANDING_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

WATCH_DIR.mkdir(parents=True, exist_ok=True)
(WATCH_DIR / "processed").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Editor — Reels factory")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")
app.mount("/branding", StaticFiles(directory=str(BRANDING_DIR)), name="branding")


# ===== Auth (sesion cookie-based, single admin) =====

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "info@ilernetwork.com").strip()
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()
AUTH_ENABLED = bool(ADMIN_PASS)
SESSION_SECRET = os.environ.get("EDITOR_SECRET") or secrets.token_hex(32)


def is_authed(request: Request) -> bool:
    if not AUTH_ENABLED:
        return True
    try:
        return request.session.get("user") == ADMIN_EMAIL
    except Exception:
        return False


class CookieAuthMiddleware(BaseHTTPMiddleware):
    """Protege rutas. Login form en /login. Static y health publicos.
    /api/run usa autenticacion por token en vez de cookie (ver endpoint)."""

    async def dispatch(self, request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)
        path = request.url.path
        if (path in {"/login", "/api/health", "/api/run"}
                or path.startswith("/static/")):
            return await call_next(request)
        if is_authed(request):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"error": "auth required"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=303)


# Orden de add_middleware: LIFO. SessionMiddleware debe envolver a CookieAuth
# para que request.session este disponible. Por eso primero anadimos
# CookieAuth (sera el "inner") y despues SessionMiddleware (sera el "outer").
app.add_middleware(CookieAuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=14 * 24 * 3600,  # 14 dias
    same_site="lax",
)

if AUTH_ENABLED:
    print(f"[auth] Login cookie-based activo (admin={ADMIN_EMAIL})")
else:
    print(f"[auth] Sin auth (define ADMIN_PASS para protegerlo)")


def yt_video_id(url: str) -> str | None:
    """Extrae un ID estable de cualquier URL soportada por yt-dlp.
    Para YouTube usa el v=ID. Para otras (TikTok/IG/Twitter/Twitch...)
    usa un hash corto del URL."""
    m = re.search(
        r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{6,15})", url
    )
    if m:
        return m.group(1)
    if not url.lower().startswith(("http://", "https://")):
        return None
    import hashlib
    return "u" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:11]


def check_anthropic_key() -> dict:
    """Devuelve estado del API key de Claude (configurada / valida / error)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return {"configured": False, "valid": None, "model": None}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key, timeout=8.0)
        model_id = os.environ.get("HIGHLIGHTS_MODEL", "claude-opus-4-7").strip()
        m = client.models.retrieve(model_id)
        return {
            "configured": True, "valid": True,
            "model": model_id,
            "model_display": getattr(m, "display_name", model_id),
        }
    except Exception as e:
        msg = str(e)
        return {
            "configured": True, "valid": False,
            "model": None,
            "error": msg[:200],
        }


# ===== Helpers =====

def _list_dir(folder: Path, exts: set[str]) -> list[str]:
    if not folder.exists():
        return []
    return sorted(
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in exts
    )


def list_videos() -> list[dict]:
    items = []
    for name in _list_dir(INPUT_DIR, VIDEO_EXTS):
        p = INPUT_DIR / name
        items.append({
            "name": name,
            "size_mb": round(p.stat().st_size / (1024 * 1024), 1),
        })
    return items


def list_profiles() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def load_job(job_id: str) -> dict | None:
    p = JOBS_DIR / f"{job_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_job(job_id: str, data: dict) -> None:
    p = JOBS_DIR / f"{job_id}.json"
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_jobs(limit: int = 12, offset: int = 0,
              search: str = "", status: str = "") -> tuple[list[dict], int]:
    """Devuelve (jobs_pagina, total). Filtra por search en video/profile y status."""
    if not JOBS_DIR.exists():
        return [], 0
    files = sorted(
        JOBS_DIR.glob("*.json"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    all_jobs: list[dict] = []
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if search:
            blob = (
                f"{d.get('video','')} {d.get('profile','')} {d.get('id','')}"
            ).lower()
            if search.lower() not in blob:
                continue
        if status and d.get("status") != status:
            continue
        all_jobs.append(d)
    total = len(all_jobs)
    return all_jobs[offset:offset + limit], total


def reset_orphan_jobs() -> None:
    """Al arrancar, marca jobs en running/queued como 'interrupted' (porque
    el subprocess se mato al reiniciar el servidor)."""
    if not JOBS_DIR.exists():
        return
    fixed = 0
    now = datetime.now().isoformat()
    for p in JOBS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("status") in ("running", "queued"):
                d["status"] = "interrupted"
                d["ended"] = d.get("ended") or now
                p.write_text(
                    json.dumps(d, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                fixed += 1
        except Exception:
            pass
    if fixed:
        print(f"[startup] {fixed} jobs huerfanos marcados como 'interrupted'")


def kill_process_tree(pid: int) -> bool:
    """Mata un proceso y todos sus hijos (cross-platform)."""
    try:
        if sys.platform == "win32":
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True,
            )
            return res.returncode == 0
        else:
            import os as _os
            import signal as _sig
            try:
                _os.killpg(_os.getpgid(pid), _sig.SIGTERM)
            except ProcessLookupError:
                return False
            return True
    except Exception as e:
        print(f"[kill] error matando PID {pid}: {e}")
        return False


reset_orphan_jobs()


def find_reels(out_dir_name: str) -> list[dict]:
    out_dir = OUTPUT_DIR / out_dir_name
    if not out_dir.exists():
        return []
    reels = []
    for mp4 in sorted(out_dir.glob("reel_*.mp4")):
        stem = mp4.stem
        thumb = out_dir / f"{stem}.jpg"
        txt = out_dir / f"{stem}.txt"
        en_srt = out_dir / f"{stem}_en.srt"
        copy_json = out_dir / f"{stem}.copy.json"
        info: dict = {"name": mp4.name, "stem": stem,
                      "video": f"/output/{out_dir_name}/{mp4.name}"}
        if thumb.exists():
            info["thumb"] = f"/output/{out_dir_name}/{thumb.name}"
        if txt.exists():
            try:
                info["txt"] = txt.read_text(encoding="utf-8")
            except Exception:
                info["txt"] = ""
        if en_srt.exists():
            info["en_srt"] = f"/output/{out_dir_name}/{en_srt.name}"
        if copy_json.exists():
            try:
                info["copy"] = json.loads(copy_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        info["size_mb"] = round(mp4.stat().st_size / (1024 * 1024), 1)
        reels.append(info)
    return reels


def find_montage(out_dir_name: str) -> str | None:
    p = OUTPUT_DIR / f"{out_dir_name}_montage.mp4"
    if p.exists():
        return f"/output/{out_dir_name}_montage.mp4"
    return None


def dir_size(path: Path) -> int:
    """Tamano total en bytes de los archivos en un directorio (recursivo)."""
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


DISK_WARN_GB = 10.0
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
WEBHOOK_PUBLIC_URL = os.environ.get("WEBHOOK_PUBLIC_URL", "").strip()


# Pricing por 1M tokens, modelo -> (input, output, cache_write_mult, cache_read_mult)
MODEL_PRICING = {
    "claude-opus-4-7":   (5.00, 25.00),
    "claude-opus-4-6":   (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
}
DEFAULT_PRICING = (5.00, 25.00)  # Opus pricing fallback


def parse_usage_from_log(log: str) -> dict | None:
    """Extrae tokens usados de la linea 'tokens: in=N out=N cache_write=N cache_read=N'."""
    m = re.search(
        r"tokens:\s*in=(\d+)\s+out=(\d+)\s+cache_write=(\d+)\s+cache_read=(\d+)",
        log,
    )
    if not m:
        return None
    return {
        "input_tokens": int(m.group(1)),
        "output_tokens": int(m.group(2)),
        "cache_write": int(m.group(3)),
        "cache_read": int(m.group(4)),
    }


def usage_cost(usage: dict, model: str = "claude-opus-4-7") -> float:
    """Calcula coste estimado en USD a partir del usage dict."""
    in_p, out_p = MODEL_PRICING.get(model, DEFAULT_PRICING)
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cw = usage.get("cache_write", 0)
    cr = usage.get("cache_read", 0)
    return (
        (inp / 1_000_000) * in_p
        + (out / 1_000_000) * out_p
        + (cw / 1_000_000) * in_p * 1.25
        + (cr / 1_000_000) * in_p * 0.10
    )


def fire_webhook(job: dict) -> None:
    """POST a WEBHOOK_URL con el resumen del job. Compatible con Slack/Discord
    (campo `text` y `content` con string para clientes simples) + payload
    estructurado para receivers genericos."""
    if not WEBHOOK_URL:
        return
    try:
        import httpx
    except ImportError:
        return
    status = job.get("status", "?")
    video = job.get("video", "?")
    n_clips = job.get("n_clips", 0)
    job_id = job.get("id", "?")
    started = job.get("started")
    ended = job.get("ended")
    dur_s = 0
    if started and ended:
        try:
            from datetime import datetime as _dt
            dur_s = int((_dt.fromisoformat(ended) - _dt.fromisoformat(started)).total_seconds())
        except Exception:
            pass
    public_link = ""
    if WEBHOOK_PUBLIC_URL:
        public_link = f"{WEBHOOK_PUBLIC_URL.rstrip('/')}/job/{job_id}"

    icon = {"done": "[OK]", "error": "[ERR]",
            "cancelled": "[CXL]", "interrupted": "[INT]"}.get(status, "[?]")
    text = (
        f"{icon} REEL/LAB: job {job_id} -> {status}\n"
        f"Video: {video}\n"
        f"Reels: {n_clips} | Duracion: {dur_s}s"
    )
    if public_link:
        text += f"\n{public_link}"

    payload = {
        # Generic + Slack-compatible
        "text": text,
        # Discord-compatible
        "content": text,
        # Structured
        "event": "job.finished",
        "job": {
            "id": job_id,
            "status": status,
            "video": video,
            "n_clips": n_clips,
            "duration_seconds": dur_s,
            "url": public_link or None,
        },
    }
    try:
        httpx.post(WEBHOOK_URL, json=payload, timeout=8.0)
        print(f"[webhook] -> {status} for {job_id}")
    except Exception as e:
        print(f"[webhook] fallo: {e}")


def collect_stats() -> dict:
    """Estadisticas globales de uso del proyecto."""
    counts = {"queued": 0, "running": 0, "done": 0, "error": 0,
              "cancelled": 0, "interrupted": 0}
    total_jobs = 0
    total_cost = 0.0
    total_in = total_out = total_cache_w = total_cache_r = 0
    api_jobs = 0
    if JOBS_DIR.exists():
        for p in JOBS_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                s = d.get("status", "unknown")
                counts[s] = counts.get(s, 0) + 1
                total_jobs += 1
                u = d.get("usage")
                if u:
                    api_jobs += 1
                    total_cost += float(u.get("cost_usd", 0) or 0)
                    total_in += int(u.get("input_tokens", 0) or 0)
                    total_out += int(u.get("output_tokens", 0) or 0)
                    total_cache_w += int(u.get("cache_write", 0) or 0)
                    total_cache_r += int(u.get("cache_read", 0) or 0)
            except Exception:
                continue

    input_bytes = dir_size(INPUT_DIR)
    output_bytes = dir_size(OUTPUT_DIR)
    music_bytes = dir_size(MUSIC_DIR)
    branding_bytes = dir_size(BRANDING_DIR)
    jobs_bytes = dir_size(JOBS_DIR)
    total_bytes = (
        input_bytes + output_bytes + music_bytes
        + branding_bytes + jobs_bytes
    )

    return {
        "jobs": {**counts, "total": total_jobs},
        "disk": {
            "input_mb": round(input_bytes / (1024 * 1024), 1),
            "output_mb": round(output_bytes / (1024 * 1024), 1),
            "music_mb": round(music_bytes / (1024 * 1024), 1),
            "branding_mb": round(branding_bytes / (1024 * 1024), 1),
            "jobs_mb": round(jobs_bytes / (1024 * 1024), 1),
            "total_mb": round(total_bytes / (1024 * 1024), 1),
            "total_gb": round(total_bytes / (1024 * 1024 * 1024), 2),
        },
        "files": {
            "videos_input": len(_list_dir(INPUT_DIR, VIDEO_EXTS)),
            "music_tracks": len(_list_dir(MUSIC_DIR, MUSIC_EXTS)),
            "watermarks": len(_list_dir(BRANDING_DIR, IMG_EXTS)),
            "profiles": len(list_profiles()),
        },
        "api_cost": {
            "jobs_with_api": api_jobs,
            "total_usd": round(total_cost, 4),
            "avg_per_job_usd": round(total_cost / api_jobs, 4) if api_jobs else 0,
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cache_write": total_cache_w,
            "cache_read": total_cache_r,
        },
    }


# ===== Pipeline runner =====

def run_pipeline_worker(job_id: str) -> None:
    job = load_job(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started"] = datetime.now().isoformat()
    save_job(job_id, job)

    script = "auto_yt.py" if job.get("use_yt") else "auto_reels_pro.py"
    cmd = [sys.executable, str(SCRIPTS_DIR / script)] + list(job["args"])
    log_path = JOBS_DIR / f"{job_id}.log"

    popen_kwargs: dict = dict(
        cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        bufsize=1,
    )
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True  # process group para killpg

    try:
        with log_path.open("w", encoding="utf-8") as logf:
            proc = subprocess.Popen(cmd, **popen_kwargs)
            # Persiste el PID para soportar /cancel
            job = load_job(job_id) or job
            job["pid"] = proc.pid
            save_job(job_id, job)
            assert proc.stdout is not None
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
            rc = proc.wait()
        job = load_job(job_id) or job
        # Si ya fue cancelado, respeta ese estado
        if job.get("status") != "cancelled":
            job["status"] = "done" if rc == 0 else "error"
        job["return_code"] = rc
        job["ended"] = datetime.now().isoformat()
        job.pop("pid", None)
        # Parsea uso del Claude API si lo hubo
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            usage = parse_usage_from_log(log_text)
            if usage:
                model = os.environ.get("HIGHLIGHTS_MODEL", "claude-opus-4-7")
                usage["model"] = model
                usage["cost_usd"] = round(usage_cost(usage, model), 5)
                job["usage"] = usage
        except Exception:
            pass
        save_job(job_id, job)
        fire_webhook(job)
    except Exception as e:
        job = load_job(job_id) or job
        job["status"] = "error"
        job["error"] = str(e)
        job["ended"] = datetime.now().isoformat()
        job.pop("pid", None)
        save_job(job_id, job)
        fire_webhook(job)


# ===== Routes =====

@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, next: str = "/"):
    if is_authed(request):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "email": ADMIN_EMAIL,
        "error": None,
        "next": next,
    })


@app.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    ok_email = secrets.compare_digest(email.strip().lower(), ADMIN_EMAIL.lower())
    ok_pass = secrets.compare_digest(password, ADMIN_PASS)
    if ok_email and ok_pass:
        request.session["user"] = ADMIN_EMAIL
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "email": email,
        "error": "Credenciales incorrectas",
        "next": next,
    }, status_code=401)


@app.post("/logout")
async def logout(request: Request):
    try:
        request.session.clear()
    except Exception:
        pass
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    recent, _ = list_jobs(limit=8)
    stats = collect_stats()
    disk_warn = stats["disk"]["total_gb"] > DISK_WARN_GB
    return templates.TemplateResponse(request, "home.html", {
        "videos": list_videos(),
        "profiles": list_profiles(),
        "music_files": _list_dir(MUSIC_DIR, MUSIC_EXTS),
        "watermarks": _list_dir(BRANDING_DIR, IMG_EXTS),
        "recent_jobs": recent,
        "recent_reels": find_recent_reels(limit=8),
        "current_user": ADMIN_EMAIL if AUTH_ENABLED and is_authed(request) else None,
        "disk_warn": disk_warn,
        "disk_total_gb": stats["disk"]["total_gb"],
        "disk_warn_threshold": DISK_WARN_GB,
    })


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    page: int = 1,
    q: str = "",
    status: str = "",
):
    page = max(1, page)
    per_page = 20
    offset = (page - 1) * per_page
    jobs, total = list_jobs(
        limit=per_page, offset=offset, search=q, status=status
    )
    pages = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse(request, "jobs.html", {
        "jobs": jobs,
        "total": total,
        "page": page,
        "pages": pages,
        "q": q,
        "status_filter": status,
        "current_user": ADMIN_EMAIL if AUTH_ENABLED and is_authed(request) else None,
    })


_PRO_PKG_README = """REEL/LAB - Pro editor handoff package
========================================

Este paquete contiene los reels en formato BRUTO + subtitulos sueltos
para que los importes en CapCut, DaVinci Resolve, Adobe Premiere, Final
Cut Pro o cualquier editor profesional / movil donde quieras pulir a mano.

ESTRUCTURA:
  reel_NN/
    reel_NN_raw.mp4      Video LIMPIO recortado a 9:16 1080x1920.
                          Sin subs, sin musica, sin watermark, sin
                          color grading, sin Ken Burns. Lienzo en blanco.
    reel_NN_burned.mp4   Como salio de REEL/LAB (con todo). Referencia.
    reel_NN.srt          Subtitulos sueltos para importar al editor.
    reel_NN.txt          Transcripcion + hashtags + copy IA.
    reel_NN.copy.json    Copy IA en formato estructurado (titulo, desc...).
    reel_NN.jpg          Miniatura.

USO EN CAPCUT (mobile):
  1. Importa reel_NN_raw.mp4
  2. Texto -> Subtitulos automaticos -> Importar SRT (selecciona .srt)
     (o pega manualmente las lineas del .srt)
  3. Aplica tu estilo de texto/animacion
  4. Anade musica de la libreria de CapCut
  5. Exporta

USO EN DAVINCI RESOLVE (gratis, profesional):
  1. Media Pool: arrastra reel_NN_raw.mp4
  2. File -> Import -> Subtitle -> selecciona reel_NN.srt
  3. Lleva ambos al timeline
  4. Color grade, audio, transiciones a tu gusto
  5. Deliver -> H.264 vertical (1080x1920, 30fps)

USO EN ADOBE PREMIERE:
  1. Project -> Import -> reel_NN_raw.mp4
  2. File -> New -> Captions -> from File -> reel_NN.srt
  3. Drag al timeline, editar y exportar

USO EN FINAL CUT PRO:
  1. Importa el .mp4 raw
  2. File -> Import -> Captions -> reel_NN.srt
  3. Editar y compartir

GENERADO POR REEL/LAB
"""


@app.get("/job/{job_id}/pro-package.zip")
async def pro_package(job_id: str):
    """Genera un ZIP con los clips RAW + SRTs sueltos para edicion manual.
    Re-renderiza cada clip en bruto (sin subs/efectos) usando ffmpeg ultrafast.
    Tarda ~5-30s por reel. Block sync (browser espera)."""
    import io
    import zipfile

    job = load_job(job_id)
    if not job or not job.get("out_dir"):
        raise HTTPException(404, "Job no existe")
    out_dir = OUTPUT_DIR / job["out_dir"]
    if not out_dir.exists():
        raise HTTPException(404, "Sin outputs")

    # Resolver source video
    first_arg = job["args"][0]
    if job.get("use_yt"):
        vid = yt_video_id(first_arg)
        source = INPUT_DIR / f"yt_{vid}.mp4"
    else:
        source = Path(first_arg)
        if not source.is_absolute():
            source = ROOT / source
    if not source.exists():
        raise HTTPException(404, f"Source video no existe: {source.name}")

    work_dir = Path(tempfile.mkdtemp(prefix="propkg_"))
    try:
        for reel_mp4 in sorted(out_dir.glob("reel_*.mp4")):
            stem = reel_mp4.stem
            segs_path = out_dir / f"{stem}.segs.json"
            if not segs_path.exists():
                continue
            try:
                sd = json.loads(segs_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            t0 = float(sd.get("t0", 0))
            clip_len = float(sd.get("clip_len", 30))

            reel_dir = work_dir / stem
            reel_dir.mkdir(parents=True, exist_ok=True)

            # Render raw 9:16 sin subs/efectos (ultrafast preset)
            raw_path = reel_dir / f"{stem}_raw.mp4"
            cmd = [
                "ffmpeg", "-y", "-ss", str(t0), "-i", str(source),
                "-t", str(clip_len),
                "-vf", "crop=ih*9/16:ih,scale=1080:1920:flags=lanczos",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(raw_path),
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=180)
            except Exception as e:
                print(f"[propkg] fallo render raw {stem}: {e}")

            # Copia el burned + thumbnail + txt + copy.json
            shutil.copy2(reel_mp4, reel_dir / f"{stem}_burned.mp4")
            for ext in (".jpg", ".txt", ".copy.json"):
                src = out_dir / f"{stem}{ext}"
                if src.exists():
                    shutil.copy2(src, reel_dir / src.name)

            # SRT desde segments
            lines = []
            for i, s in enumerate(sd.get("segments", []), 1):
                lines.append(str(i))
                lines.append(f"{_srt_time(s['start'])} --> {_srt_time(s['end'])}")
                lines.append(s["text"])
                lines.append("")
            (reel_dir / f"{stem}.srt").write_text(
                "\n".join(lines), encoding="utf-8"
            )

        (work_dir / "README.txt").write_text(_PRO_PKG_README, encoding="utf-8")

        # Build ZIP
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=4) as zf:
            for p in sorted(work_dir.rglob("*")):
                if p.is_file():
                    zf.write(p, arcname=str(p.relative_to(work_dir)))
        buf.seek(0)
        fname = f"{job['out_dir']}_propkg.zip"
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.get("/job/{job_id}/zip")
async def job_zip(job_id: str):
    """Descarga todos los outputs del job como ZIP."""
    import io
    import zipfile

    job = load_job(job_id)
    if not job or not job.get("out_dir"):
        raise HTTPException(404, "Job no existe")
    out_dir = OUTPUT_DIR / job["out_dir"]
    if not out_dir.exists():
        raise HTTPException(404, "No hay outputs")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(out_dir.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(out_dir)))
        montage = OUTPUT_DIR / f"{job['out_dir']}_montage.mp4"
        if montage.exists():
            zf.write(montage, arcname=f"_montage/{montage.name}")
    buf.seek(0)
    fname = f"{job['out_dir']}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    safe_name = (file.filename or "video.mp4").replace("\\", "/").split("/")[-1]
    suffix = Path(safe_name).suffix.lower()
    if suffix not in VIDEO_EXTS:
        raise HTTPException(400, f"Extension no soportada: {suffix}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = INPUT_DIR / f"{timestamp}_{safe_name}"
    written = 0
    with dest.open("wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    return {"name": dest.name, "size_mb": round(written / (1024 * 1024), 1)}


@app.post("/upload-asset")
async def upload_asset(file: UploadFile = File(...), kind: str = Form(...)):
    """Sube un asset (musica o watermark) a music/ o branding/."""
    if kind == "music":
        target_dir = MUSIC_DIR
        valid_exts = MUSIC_EXTS
    elif kind == "watermark":
        target_dir = BRANDING_DIR
        valid_exts = IMG_EXTS
    else:
        raise HTTPException(400, "kind debe ser 'music' o 'watermark'")

    safe_name = (file.filename or "asset").replace("\\", "/").split("/")[-1]
    if Path(safe_name).suffix.lower() not in valid_exts:
        raise HTTPException(400, f"Extension no valida para {kind}")

    dest = target_dir / safe_name
    with dest.open("wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return {"name": dest.name}


@app.post("/run")
async def run_job(
    video: str = Form(""),
    url: str = Form(""),
    n_clips: int = Form(6),
    profile: str = Form(""),
    style: str = Form("clean"),
    grade: str = Form("none"),
    duration: float = Form(35),
    chunk: str = Form("3"),
    music: str = Form(""),
    music_vol: float = Form(0.18),
    duck: str = Form(""),
    watermark: str = Form(""),
    watermark_pos: str = Form("br"),
    watermark_scale: float = Form(12),
    outro: str = Form(""),
    outro_duration: float = Form(1.5),
    skip_start: float = Form(0),
    skip_end: float = Form(0),
    no_hook: str = Form(""),
    translate_en: str = Form(""),
    ai_highlights: str = Form(""),
    instructions: str = Form(""),
    no_normalize: str = Form(""),
    karaoke: str = Form(""),
    generate_copy: str = Form(""),
    denoise: str = Form(""),
):
    url = url.strip()
    if not video and not url:
        raise HTTPException(400, "Tienes que especificar un video O una URL")

    # Bulk: multiples URLs separadas por linea
    urls_list = [
        u.strip() for u in url.splitlines() if u.strip().startswith("http")
    ]
    if len(urls_list) > 1:
        # Crea N jobs, redirige a /jobs
        return _spawn_bulk_jobs(
            urls_list, n_clips, profile, style, grade, duration, chunk,
            music, music_vol, duck, watermark, watermark_pos, watermark_scale,
            outro, outro_duration, skip_start, skip_end, no_hook,
            translate_en, ai_highlights, instructions, no_normalize, karaoke,
            generate_copy,
        )

    use_yt = bool(url)
    if use_yt:
        vid = yt_video_id(url)
        if not vid:
            raise HTTPException(400, "URL de YouTube no reconocida")
        video_stem = f"yt_{vid}"
        out_dir_name = f"{video_stem}_pro"
        # auto_yt acepta: URL n_clips [resto de flags pasa a auto_reels_pro]
        args: list[str] = [url, str(int(n_clips))]
    else:
        video_path = INPUT_DIR / video
        if not video_path.exists():
            raise HTTPException(404, f"Video no existe: {video}")
        out_dir_name = f"{video_path.stem}_pro"
        args = [str(video_path), str(int(n_clips))]
    if profile:
        args += ["--profile", profile]
    else:
        if style and style != "clean":
            args += ["--style", style]
        if grade and grade != "none":
            args += ["--grade", grade]
        if duration and float(duration) != 35.0:
            args += ["--duration", str(duration)]
        if chunk and chunk != "3":
            args += ["--chunk", str(chunk)]
    if music:
        args += ["--music", str(MUSIC_DIR / music)]
        if music_vol and float(music_vol) != 0.18:
            args += ["--music-vol", str(music_vol)]
    if duck == "on":
        args += ["--duck"]
    if watermark:
        args += ["--watermark", str(BRANDING_DIR / watermark),
                 "--watermark-pos", watermark_pos]
        if float(watermark_scale) != 12.0:
            args += ["--watermark-scale", str(watermark_scale)]
    if outro.strip():
        args += ["--outro", outro,
                 "--outro-duration", str(outro_duration)]
    if float(skip_start) > 0:
        args += ["--skip-start", str(skip_start)]
    if float(skip_end) > 0:
        args += ["--skip-end", str(skip_end)]
    if no_hook == "on":
        args += ["--no-hook"]
    if translate_en == "on":
        args += ["--translate-en"]
    if ai_highlights == "on":
        args += ["--ai-highlights"]
    if instructions and instructions.strip():
        args += ["--instructions", instructions.strip()]
    if no_normalize == "on":
        args += ["--no-normalize"]
    if karaoke == "on":
        args += ["--karaoke"]
    if generate_copy == "on":
        args += ["--generate-copy"]
    if denoise == "on":
        args += ["--denoise"]

    job_id = uuid.uuid4().hex[:12]
    job_data = {
        "id": job_id,
        "video": video if not use_yt else f"(URL) {url[:60]}",
        "url": url if use_yt else None,
        "use_yt": use_yt,
        "n_clips": int(n_clips),
        "profile": profile or None,
        "args": args,
        "status": "queued",
        "created": datetime.now().isoformat(),
        "started": None,
        "ended": None,
        "out_dir": out_dir_name,
    }
    save_job(job_id, job_data)

    threading.Thread(target=run_pipeline_worker, args=(job_id,), daemon=True).start()
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.get("/job/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(404, "Job no existe")
    reels = find_reels(job["out_dir"]) if job.get("out_dir") else []
    montage_url = find_montage(job["out_dir"]) if job.get("out_dir") else None
    return templates.TemplateResponse(request, "job.html", {
        "job": job,
        "reels": reels,
        "montage_url": montage_url,
    })


@app.post("/job/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(404, "Job no existe")
    if job.get("status") not in ("running", "queued"):
        return JSONResponse({"error": f"Job esta {job.get('status')}"},
                            status_code=400)
    pid = job.get("pid")
    if pid:
        kill_process_tree(int(pid))
    job["status"] = "cancelled"
    job["ended"] = datetime.now().isoformat()
    save_job(job_id, job)
    return JSONResponse({"status": "cancelled"})


# ===== Profile editor =====

def _read_profile_args(name: str) -> list:
    p = PROFILES_DIR / f"{name}.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data]
        if isinstance(data, dict) and "args" in data:
            return [str(x) for x in data["args"]]
    except Exception:
        pass
    return []


def _args_to_text(args: list) -> str:
    """Convierte ['--style','hype','--grade','vivid'] -> texto multilinea."""
    out = []
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("--"):
            i += 1
            continue
        is_bool = a in {"--equal", "--no-hook", "--duck", "--translate-en"}
        if is_bool or i + 1 >= len(args) or args[i + 1].startswith("--"):
            out.append(a)
            i += 1
        else:
            out.append(f"{a} {args[i + 1]}")
            i += 2
    return "\n".join(out)


def _text_to_args(text: str) -> list:
    """Convierte texto multilinea -> lista de args."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        flag = parts[0]
        if not flag.startswith("--"):
            continue
        if len(parts) == 1:
            out.append(flag)
        else:
            out.append(flag)
            out.append(parts[1].strip())
    return out


@app.get("/profiles", response_class=HTMLResponse)
async def profiles_page(request: Request, edit: str = ""):
    profiles = list_profiles()
    active = edit if edit in profiles else (edit if edit == "" else "")
    is_new = (edit == "" and not active) or (edit != "" and edit not in profiles)
    args = _read_profile_args(active) if active in profiles else []
    flags_text = _args_to_text(args)
    return templates.TemplateResponse(request, "profiles.html", {
        "profiles": profiles,
        "active": active,
        "is_new": is_new,
        "flags_text": flags_text,
    })


@app.post("/profiles/save")
async def profiles_save(
    name: str = Form(...),
    flags_text: str = Form(""),
):
    safe = "".join(c for c in name if c.isalnum() or c in ("_", "-"))
    if not safe:
        raise HTTPException(400, "Nombre invalido (solo letras/numeros/-_)")
    args = _text_to_args(flags_text)
    p = PROFILES_DIR / f"{safe}.json"
    p.write_text(json.dumps(args, indent=4, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    return RedirectResponse(f"/profiles?edit={safe}", status_code=303)


@app.post("/profiles/{name}/delete")
async def profiles_delete(name: str):
    safe = "".join(c for c in name if c.isalnum() or c in ("_", "-"))
    p = PROFILES_DIR / f"{safe}.json"
    if p.exists():
        p.unlink()
    return RedirectResponse("/profiles", status_code=303)


def _build_pipeline_args(
    n_clips: int, profile: str, style: str, grade: str, duration: float,
    chunk: str, music: str, music_vol: float, duck: str,
    watermark: str, watermark_pos: str, watermark_scale: float,
    outro: str, outro_duration: float, skip_start: float, skip_end: float,
    no_hook: str, translate_en: str, ai_highlights: str,
    instructions: str = "", no_normalize: str = "", karaoke: str = "",
    generate_copy: str = "", denoise: str = "",
) -> list[str]:
    """Genera la lista de flags para auto_reels_pro a partir del form."""
    args: list[str] = []
    if profile:
        args += ["--profile", profile]
    else:
        if style and style != "clean":
            args += ["--style", style]
        if grade and grade != "none":
            args += ["--grade", grade]
        if duration and float(duration) != 35.0:
            args += ["--duration", str(duration)]
        if chunk and chunk != "3":
            args += ["--chunk", str(chunk)]
    if music:
        args += ["--music", str(MUSIC_DIR / music)]
        if music_vol and float(music_vol) != 0.18:
            args += ["--music-vol", str(music_vol)]
    if duck == "on":
        args += ["--duck"]
    if watermark:
        args += ["--watermark", str(BRANDING_DIR / watermark),
                 "--watermark-pos", watermark_pos]
        if float(watermark_scale) != 12.0:
            args += ["--watermark-scale", str(watermark_scale)]
    if outro and outro.strip():
        args += ["--outro", outro,
                 "--outro-duration", str(outro_duration)]
    if float(skip_start) > 0:
        args += ["--skip-start", str(skip_start)]
    if float(skip_end) > 0:
        args += ["--skip-end", str(skip_end)]
    if no_hook == "on":
        args += ["--no-hook"]
    if translate_en == "on":
        args += ["--translate-en"]
    if ai_highlights == "on":
        args += ["--ai-highlights"]
    if instructions and instructions.strip():
        args += ["--instructions", instructions.strip()]
    if no_normalize == "on":
        args += ["--no-normalize"]
    if karaoke == "on":
        args += ["--karaoke"]
    if generate_copy == "on":
        args += ["--generate-copy"]
    if denoise == "on":
        args += ["--denoise"]
    return args


def _spawn_bulk_jobs(
    urls: list[str], n_clips: int, profile: str, style: str, grade: str,
    duration: float, chunk: str, music: str, music_vol: float, duck: str,
    watermark: str, watermark_pos: str, watermark_scale: float,
    outro: str, outro_duration: float, skip_start: float, skip_end: float,
    no_hook: str, translate_en: str, ai_highlights: str,
    instructions: str = "", no_normalize: str = "", karaoke: str = "",
    generate_copy: str = "", denoise: str = "",
) -> RedirectResponse:
    """Crea N jobs en cola (uno por URL) y redirige al listado."""
    base_args = _build_pipeline_args(
        n_clips, profile, style, grade, duration, chunk,
        music, music_vol, duck, watermark, watermark_pos, watermark_scale,
        outro, outro_duration, skip_start, skip_end, no_hook,
        translate_en, ai_highlights, instructions, no_normalize, karaoke,
        generate_copy, denoise,
    )
    spawned = 0
    for u in urls:
        vid = yt_video_id(u)
        if not vid:
            continue
        job_args = [u, str(int(n_clips))] + base_args
        job_id = uuid.uuid4().hex[:12]
        save_job(job_id, {
            "id": job_id,
            "video": f"(URL bulk) {u[:60]}",
            "url": u,
            "use_yt": True,
            "n_clips": int(n_clips),
            "profile": profile or None,
            "args": job_args,
            "status": "queued",
            "created": datetime.now().isoformat(),
            "started": None,
            "ended": None,
            "out_dir": f"yt_{vid}_pro",
        })
        threading.Thread(
            target=run_pipeline_worker, args=(job_id,), daemon=True
        ).start()
        spawned += 1
    print(f"[bulk] {spawned} jobs encolados desde {len(urls)} URLs")
    return RedirectResponse(f"/jobs?q=bulk", status_code=303)


def _srt_time(s: float) -> str:
    h = int(s // 3600); m = int((s % 3600) // 60)
    sec = int(s % 60); ms = int((s - int(s)) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


@app.get("/job/{job_id}/edit/{reel_id}", response_class=HTMLResponse)
async def edit_reel(request: Request, job_id: str, reel_id: int):
    job = load_job(job_id)
    if not job:
        raise HTTPException(404)
    out_dir = OUTPUT_DIR / job["out_dir"]
    segs_path = out_dir / f"reel_{reel_id:02d}.segs.json"
    if not segs_path.exists():
        raise HTTPException(404, "Sin segmentos guardados (job antiguo, re-genera)")
    seg_data = json.loads(segs_path.read_text(encoding="utf-8"))
    return templates.TemplateResponse(request, "edit_reel.html", {
        "job": job,
        "reel_id": reel_id,
        "seg_data": seg_data,
        "languages": SUPPORTED_TRANSLATIONS,
        "current_user": ADMIN_EMAIL if AUTH_ENABLED and is_authed(request) else None,
    })


@app.post("/job/{job_id}/edit/{reel_id}")
async def edit_reel_save(
    job_id: str, reel_id: int,
    segments_text: str = Form(...),
):
    """Guarda transcripcion editada. Una linea por segmento, formato:
        START|END|TEXTO
    Ej:  0.0|3.5|Hola que tal amigos
    """
    job = load_job(job_id)
    if not job:
        raise HTTPException(404)
    out_dir = OUTPUT_DIR / job["out_dir"]
    segs_path = out_dir / f"reel_{reel_id:02d}.segs.json"
    if not segs_path.exists():
        raise HTTPException(404)
    seg_data = json.loads(segs_path.read_text(encoding="utf-8"))

    new_segments = []
    for line in segments_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        try:
            start = float(parts[0].strip())
            end = float(parts[1].strip())
            text = parts[2].strip()
            if text:
                new_segments.append({"start": start, "end": end, "text": text})
        except ValueError:
            continue

    seg_data["segments"] = new_segments
    seg_data["edited"] = True
    seg_data["edited_at"] = datetime.now().isoformat()
    segs_path.write_text(
        json.dumps(seg_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return RedirectResponse(
        f"/job/{job_id}/edit/{reel_id}?saved=1", status_code=303
    )


@app.post("/job/{job_id}/edit/{reel_id}/reburn")
async def reburn_reel_endpoint(job_id: str, reel_id: int):
    """Re-renderiza el reel N usando los segments editados (segs.json) y
    los mismos flags de estilo del job original."""
    job = load_job(job_id)
    if not job:
        raise HTTPException(404, "Job no existe")
    out_dir = OUTPUT_DIR / job["out_dir"]
    segs_path = out_dir / f"reel_{reel_id:02d}.segs.json"
    if not segs_path.exists():
        raise HTTPException(404, "Sin segmentos guardados")
    seg_data = json.loads(segs_path.read_text(encoding="utf-8"))

    # Resolver source video
    first_arg = job["args"][0]
    if job.get("use_yt"):
        vid = yt_video_id(first_arg)
        if not vid:
            raise HTTPException(400, "URL invalida en el job")
        source = INPUT_DIR / f"yt_{vid}.mp4"
    else:
        source = Path(first_arg)
        if not source.is_absolute():
            source = ROOT / source

    if not source.exists():
        raise HTTPException(404, f"Source video no existe: {source.name}")

    # Extraer flags de estilo del job["args"] originales
    orig_args = job["args"][2:]  # skip [video, n_clips]
    pass_value = {"--style", "--grade", "--chunk", "--music", "--music-vol",
                  "--watermark", "--watermark-pos", "--watermark-scale",
                  "--outro", "--outro-duration"}
    pass_bool = {"--duck", "--no-hook", "--no-normalize", "--karaoke"}

    style_args: list[str] = []
    i = 0
    while i < len(orig_args):
        a = orig_args[i]
        if a in pass_value and i + 1 < len(orig_args):
            style_args += [a, orig_args[i + 1]]
            i += 2
        elif a in pass_bool:
            style_args += [a]
            i += 1
        else:
            i += 1

    # Si el job usaba --profile, expanderlo a flags reales
    profile_name = None
    for j, a in enumerate(orig_args):
        if a == "--profile" and j + 1 < len(orig_args):
            profile_name = orig_args[j + 1]
            break
    if profile_name:
        prof_path = PROFILES_DIR / f"{profile_name}.json"
        if prof_path.exists():
            try:
                pdata = json.loads(prof_path.read_text(encoding="utf-8"))
                pargs = pdata if isinstance(pdata, list) else pdata.get("args", [])
                k = 0
                while k < len(pargs):
                    a = pargs[k]
                    if a in pass_value and k + 1 < len(pargs):
                        # Solo anadir si no estaba ya en style_args (CLI gana)
                        if a not in style_args:
                            style_args += [a, pargs[k + 1]]
                        k += 2
                    elif a in pass_bool:
                        if a not in style_args:
                            style_args += [a]
                        k += 1
                    else:
                        k += 1
            except Exception:
                pass

    out_mp4 = out_dir / f"reel_{reel_id:02d}.mp4"
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "reburn_reel.py"),
        "--source", str(source),
        "--t0", str(seg_data.get("t0", 0)),
        "--t1", str(seg_data.get("t1", 30)),
        "--segs", str(segs_path),
        "--output", str(out_mp4),
    ] + style_args

    print(f"[reburn] reel {reel_id} de job {job_id}: {' '.join(cmd[2:6])}")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    if proc.returncode != 0:
        raise HTTPException(
            500,
            f"Re-burn fallo: {(proc.stderr or proc.stdout or '')[-500:]}",
        )
    return RedirectResponse(
        f"/job/{job_id}/edit/{reel_id}?reburned=1", status_code=303
    )


SUPPORTED_TRANSLATIONS = {
    "en": "English", "es": "Spanish", "ca": "Catalan", "fr": "French",
    "de": "German", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
    "ru": "Russian", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese (simplified)", "ar": "Arabic", "tr": "Turkish",
    "pl": "Polish", "sv": "Swedish", "id": "Indonesian", "vi": "Vietnamese",
    "hi": "Hindi", "th": "Thai",
}


def translate_segments_with_claude(segments: list, target_lang_code: str,
                                   source_lang: str = "es") -> list | None:
    """Traduce los textos de los segmentos preservando timing. Devuelve la
    lista de segmentos traducidos o None si falla."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not segments:
        return None
    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError:
        return None

    target_name = SUPPORTED_TRANSLATIONS.get(target_lang_code, target_lang_code)
    numbered = "\n".join(
        f"{i + 1}|{s['text']}" for i, s in enumerate(segments)
    )

    system = (
        f"Eres traductor profesional de subtitulos para video corto. "
        f"Traduces de {source_lang} a {target_name}. "
        f"Preservas el TONO conversacional y la estructura de cada segmento "
        f"(no fusionas ni partes). Mantienes nombres propios y marcas como "
        f"vienen. Si una linea es interjeccion ('eh', 'mmm'), la traduces "
        f"al equivalente cultural del idioma destino."
    )
    user = (
        f"Traduce estos subtitulos de {source_lang} a {target_name}. "
        f"Devuelveme una lista con el MISMO numero de items, en el mismo "
        f"orden. Cada item es {{id, translated_text}}.\n\n{numbered}"
    )

    class TransItem(BaseModel):
        id: int
        translated_text: str

    class TransBatch(BaseModel):
        items: list[TransItem]

    model_id = os.environ.get("HIGHLIGHTS_MODEL", "claude-opus-4-7").strip()
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.parse(
            model=model_id,
            max_tokens=8192,
            thinking={"type": "adaptive"},
            cache_control={"type": "ephemeral"},
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=TransBatch,
        )
    except Exception as e:
        print(f"[!!] translate fallo: {e}")
        return None

    out: list[dict] = []
    by_id = {it.id: it.translated_text for it in response.parsed_output.items}
    for i, s in enumerate(segments):
        new_text = by_id.get(i + 1, s["text"])
        out.append({"start": s["start"], "end": s["end"], "text": new_text})
    return out


@app.get("/job/{job_id}/translate/{reel_id}")
async def translate_reel(job_id: str, reel_id: int, lang: str = "en"):
    """Devuelve un .srt con la transcripcion traducida al idioma indicado.
    Lo traduce con Claude. Necesita ANTHROPIC_API_KEY."""
    job = load_job(job_id)
    if not job:
        raise HTTPException(404)
    if lang not in SUPPORTED_TRANSLATIONS:
        raise HTTPException(400, f"Idioma no soportado: {lang}")
    out_dir = OUTPUT_DIR / job["out_dir"]
    segs_path = out_dir / f"reel_{reel_id:02d}.segs.json"
    if not segs_path.exists():
        raise HTTPException(404, "Sin segmentos guardados")
    seg_data = json.loads(segs_path.read_text(encoding="utf-8"))
    src_lang = seg_data.get("language", "es")

    translated = translate_segments_with_claude(
        seg_data.get("segments", []), lang, src_lang
    )
    if not translated:
        raise HTTPException(
            500,
            "Traduccion fallo (sin ANTHROPIC_API_KEY o error de API)",
        )

    lines = []
    for i, s in enumerate(translated, 1):
        lines.append(str(i))
        lines.append(f"{_srt_time(s['start'])} --> {_srt_time(s['end'])}")
        lines.append(s["text"])
        lines.append("")
    content = "\n".join(lines)
    fname = f"reel_{reel_id:02d}_{lang}.srt"
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/job/{job_id}/srt/{reel_id}")
async def download_srt(job_id: str, reel_id: int):
    """Descarga .srt generado a partir de los segmentos (editados o no)."""
    job = load_job(job_id)
    if not job:
        raise HTTPException(404)
    out_dir = OUTPUT_DIR / job["out_dir"]
    segs_path = out_dir / f"reel_{reel_id:02d}.segs.json"
    if not segs_path.exists():
        raise HTTPException(404, "Sin segmentos guardados (job antiguo)")
    seg_data = json.loads(segs_path.read_text(encoding="utf-8"))

    lines = []
    for i, s in enumerate(seg_data.get("segments", []), 1):
        lines.append(str(i))
        lines.append(f"{_srt_time(s['start'])} --> {_srt_time(s['end'])}")
        lines.append(s["text"])
        lines.append("")
    content = "\n".join(lines)
    fname = f"reel_{reel_id:02d}.srt"
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/job/{job_id}/rerun")
async def rerun_job(job_id: str):
    """Lanza un job nuevo con exactamente los mismos args que el anterior."""
    job = load_job(job_id)
    if not job:
        raise HTTPException(404, "Job no existe")
    new_job_id = uuid.uuid4().hex[:12]
    save_job(new_job_id, {
        "id": new_job_id,
        "video": f"{job['video']} (re-run)",
        "url": job.get("url"),
        "use_yt": job.get("use_yt", False),
        "n_clips": job["n_clips"],
        "profile": job.get("profile"),
        "args": list(job["args"]),
        "status": "queued",
        "created": datetime.now().isoformat(),
        "started": None,
        "ended": None,
        "out_dir": job["out_dir"],
        "rerun_of": job_id,
    })
    threading.Thread(
        target=run_pipeline_worker, args=(new_job_id,), daemon=True
    ).start()
    return RedirectResponse(f"/job/{new_job_id}", status_code=303)


@app.post("/job/{job_id}/variant")
async def make_variant(
    job_id: str,
    new_style: str = Form(...),
    new_grade: str = Form("none"),
):
    """Crea un job nuevo con el mismo video/n_clips/etc pero distinto estilo+grade.
    Output va a un dir distinto para no pisar el original."""
    job = load_job(job_id)
    if not job:
        raise HTTPException(404, "Job no existe")
    if new_style not in {"clean", "hype", "money"}:
        raise HTTPException(400, f"Estilo invalido: {new_style}")
    if new_grade not in {"none", "warm", "cold", "cinematic", "vivid"}:
        raise HTTPException(400, f"Grade invalido: {new_grade}")

    # Limpia args originales: quita --profile, --style, --grade, --out-suffix
    base = list(job["args"])
    cleaned: list[str] = []
    skip = 0
    for i, a in enumerate(base):
        if skip > 0:
            skip -= 1
            continue
        if a in ("--profile", "--style", "--grade", "--out-suffix"):
            skip = 1
            continue
        cleaned.append(a)
    suffix = f"_{new_style}_{new_grade}"
    new_args = cleaned + [
        "--style", new_style,
        "--grade", new_grade,
        "--out-suffix", suffix,
    ]

    new_job_id = uuid.uuid4().hex[:12]
    new_job_data = {
        "id": new_job_id,
        "video": f"{job['video']} (variante {new_style}/{new_grade})",
        "url": job.get("url"),
        "use_yt": job.get("use_yt", False),
        "n_clips": job["n_clips"],
        "profile": None,
        "args": new_args,
        "status": "queued",
        "created": datetime.now().isoformat(),
        "started": None,
        "ended": None,
        "out_dir": f"{job['out_dir']}{suffix}",
        "variant_of": job_id,
    }
    save_job(new_job_id, new_job_data)
    threading.Thread(
        target=run_pipeline_worker, args=(new_job_id,), daemon=True
    ).start()
    return RedirectResponse(f"/job/{new_job_id}", status_code=303)


@app.post("/job/{job_id}/montage")
async def make_montage(
    job_id: str,
    per_clip: float = Form(6.0),
    xfade: float = Form(0.4),
):
    job = load_job(job_id)
    if not job or not job.get("out_dir"):
        raise HTTPException(404, "Job no existe")
    out_dir = OUTPUT_DIR / job["out_dir"]
    if not out_dir.exists():
        raise HTTPException(400, "No hay reels generados aun")
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "auto_montage.py"),
        str(out_dir),
        "--per-clip", str(per_clip),
        "--xfade", str(xfade),
    ]
    res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if res.returncode != 0:
        raise HTTPException(
            500,
            f"auto_montage fallo: {res.stderr[-500:] or res.stdout[-500:]}",
        )
    return RedirectResponse(f"/job/{job_id}", status_code=303)


def parse_progress(log: str, total_reels: int, status: str) -> dict:
    """Extrae progreso estructurado del log de auto_reels_pro."""
    if status == "done":
        return {"stage": "done", "label": "Completado",
                "percent": 100, "current": total_reels, "total": total_reels}
    if status in ("error", "cancelled", "interrupted"):
        return {"stage": status, "label": status,
                "percent": 0, "current": 0, "total": total_reels}

    completed_reels = [
        int(m.group(1)) for m in re.finditer(r"\[OK\] reel_(\d+)\.mp4", log)
    ]
    completed = max(completed_reels) if completed_reels else 0

    reel_markers = re.findall(r"=== REEL (\d+)/(\d+)", log)
    if reel_markers:
        cur, tot = int(reel_markers[-1][0]), int(reel_markers[-1][1])
        if completed >= cur:
            base = 40 + int((completed / tot) * 55)
            return {"stage": "rendering",
                    "label": f"Renderizando ({completed}/{tot})",
                    "percent": min(95, base),
                    "current": completed, "total": tot}
        base = 40 + int(((cur - 1) / tot) * 55) + 5
        return {"stage": "rendering",
                "label": f"Renderizando reel {cur}/{tot}",
                "percent": min(95, base),
                "current": cur, "total": tot}

    if "Modo AI: " in log or "Modo smart:" in log or "Modo equal:" in log:
        return {"stage": "selecting", "label": "Eligiendo highlights",
                "percent": 38, "current": 0, "total": total_reels}
    if "Generando transcripcion EN" in log:
        return {"stage": "translating", "label": "Generando subs EN",
                "percent": 35, "current": 0, "total": total_reels}
    if "Transcribiendo TODO" in log or "Transcribiendo" in log:
        return {"stage": "transcribing", "label": "Transcribiendo audio",
                "percent": 22, "current": 0, "total": total_reels}
    if "Cargando Whisper" in log:
        return {"stage": "loading", "label": "Cargando modelo Whisper",
                "percent": 10, "current": 0, "total": total_reels}
    if "Bajando con yt-dlp" in log:
        return {"stage": "downloading", "label": "Descargando video",
                "percent": 5, "current": 0, "total": total_reels}
    return {"stage": "starting", "label": "Arrancando...",
            "percent": 2, "current": 0, "total": total_reels}


@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(404)
    log_path = JOBS_DIR / f"{job_id}.log"
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    reels = find_reels(job.get("out_dir", "")) if job.get("status") == "done" else []
    progress = parse_progress(log, int(job.get("n_clips", 1)), job["status"])
    return JSONResponse({
        "id": job["id"],
        "status": job["status"],
        "log": log[-4000:],
        "ended": job.get("ended"),
        "reels": reels,
        "progress": progress,
    })


@app.get("/api/health")
async def health():
    return {"ok": True, "version": "1.0"}


# ===== API tokens =====

def _load_tokens() -> list[dict]:
    if not API_TOKENS_FILE.exists():
        return []
    try:
        return json.loads(API_TOKENS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_tokens(toks: list[dict]) -> None:
    API_TOKENS_FILE.write_text(
        json.dumps(toks, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _hash_token(plain: str) -> str:
    import hashlib
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def create_api_token(name: str) -> str:
    """Crea un token nuevo. Devuelve el plaintext (mostrar 1 vez al usuario)."""
    plain = secrets.token_hex(24)
    toks = _load_tokens()
    toks.append({
        "id": "tok_" + secrets.token_hex(6),
        "name": name,
        "hash": _hash_token(plain),
        "created": datetime.now().isoformat(),
        "last_used": None,
    })
    _save_tokens(toks)
    return plain


def verify_api_token(plain: str) -> dict | None:
    h = _hash_token(plain)
    toks = _load_tokens()
    for t in toks:
        if secrets.compare_digest(t.get("hash", ""), h):
            t["last_used"] = datetime.now().isoformat()
            _save_tokens(toks)
            return t
    return None


@app.get("/tokens", response_class=HTMLResponse)
async def tokens_page(request: Request, new: str = ""):
    return templates.TemplateResponse(request, "tokens.html", {
        "tokens": _load_tokens(),
        "new_token": new,
        "current_user": ADMIN_EMAIL if AUTH_ENABLED and is_authed(request) else None,
    })


@app.post("/tokens/create")
async def tokens_create(name: str = Form(...)):
    safe_name = name.strip()[:60]
    if not safe_name:
        raise HTTPException(400, "Nombre invalido")
    plain = create_api_token(safe_name)
    return RedirectResponse(f"/tokens?new={plain}", status_code=303)


@app.post("/tokens/{tok_id}/revoke")
async def tokens_revoke(tok_id: str):
    toks = [t for t in _load_tokens() if t["id"] != tok_id]
    _save_tokens(toks)
    return RedirectResponse("/tokens", status_code=303)


@app.post("/api/run")
async def api_run(request: Request):
    """Endpoint programatico para crear jobs. Auth via X-API-Token header
    o ?token= query param. Body JSON con {url|video, n_clips, profile, ...}.

    Si AUTH_ENABLED=False, este endpoint NO requiere token (modo local)."""
    if AUTH_ENABLED:
        token = (
            request.headers.get("x-api-token", "").strip()
            or request.query_params.get("token", "").strip()
        )
        if not token or not verify_api_token(token):
            return JSONResponse(
                {"error": "Invalid or missing API token (use X-API-Token header)"},
                status_code=401,
            )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Body debe ser JSON"}, status_code=400)

    url = (body.get("url") or "").strip()
    video = (body.get("video") or "").strip()
    n_clips = int(body.get("n_clips", 6))
    profile = body.get("profile") or ""
    instructions = body.get("instructions") or ""
    ai_highlights = "on" if body.get("ai_highlights") else ""
    style = body.get("style") or "clean"
    grade = body.get("grade") or "none"

    if not url and not video:
        return JSONResponse({"error": "Falta url o video"}, status_code=400)

    base_args = []
    if profile:
        base_args += ["--profile", profile]
    else:
        if style != "clean":
            base_args += ["--style", style]
        if grade != "none":
            base_args += ["--grade", grade]
    if ai_highlights:
        base_args += ["--ai-highlights"]
    if instructions:
        base_args += ["--instructions", instructions]

    use_yt = bool(url)
    if use_yt:
        vid = yt_video_id(url)
        if not vid:
            return JSONResponse({"error": "URL no reconocida"}, status_code=400)
        out_dir_name = f"yt_{vid}_pro"
        args = [url, str(n_clips)] + base_args
        video_label = f"(URL) {url[:60]}"
    else:
        video_path = INPUT_DIR / video
        if not video_path.exists():
            return JSONResponse({"error": f"Video no existe: {video}"}, status_code=404)
        out_dir_name = f"{video_path.stem}_pro"
        args = [str(video_path), str(n_clips)] + base_args
        video_label = video

    job_id = uuid.uuid4().hex[:12]
    save_job(job_id, {
        "id": job_id,
        "video": video_label,
        "url": url if use_yt else None,
        "use_yt": use_yt,
        "n_clips": n_clips,
        "profile": profile or None,
        "args": args,
        "status": "queued",
        "created": datetime.now().isoformat(),
        "started": None,
        "ended": None,
        "out_dir": out_dir_name,
        "via_api": True,
    })
    threading.Thread(
        target=run_pipeline_worker, args=(job_id,), daemon=True
    ).start()
    return JSONResponse({
        "job_id": job_id,
        "status": "queued",
        "url_status": f"/api/job/{job_id}",
        "url_view": f"/job/{job_id}",
    })


# ===== Scheduler (APScheduler) =====

def _load_schedules() -> list[dict]:
    if not SCHEDULES_FILE.exists():
        return []
    try:
        return json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_schedules(scheds: list[dict]) -> None:
    SCHEDULES_FILE.write_text(
        json.dumps(scheds, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _spawn_scheduled_job(sched_id: str) -> None:
    """Crea un job nuevo en cola para una schedule dada."""
    scheds = _load_schedules()
    sched = next((s for s in scheds if s["id"] == sched_id), None)
    if not sched or not sched.get("enabled", True):
        return
    if not sched.get("url"):
        print(f"[sched] {sched_id} sin URL")
        return
    base_args = []
    if sched.get("profile"):
        base_args += ["--profile", sched["profile"]]
    if sched.get("ai_highlights"):
        base_args += ["--ai-highlights"]
    if sched.get("instructions"):
        base_args += ["--instructions", sched["instructions"]]
    n_clips = int(sched.get("n_clips", 6))
    args = [sched["url"], str(n_clips)] + base_args
    vid = yt_video_id(sched["url"])
    if not vid:
        print(f"[sched] URL invalida en {sched_id}")
        return
    job_id = uuid.uuid4().hex[:12]
    save_job(job_id, {
        "id": job_id,
        "video": f"(scheduled '{sched.get('name','')}') {sched['url'][:60]}",
        "url": sched["url"],
        "use_yt": True,
        "n_clips": n_clips,
        "profile": sched.get("profile") or None,
        "args": args,
        "status": "queued",
        "created": datetime.now().isoformat(),
        "started": None,
        "ended": None,
        "out_dir": f"yt_{vid}_pro",
        "scheduled_by": sched_id,
    })
    threading.Thread(
        target=run_pipeline_worker, args=(job_id,), daemon=True
    ).start()
    # Update last_run
    sched["last_run"] = datetime.now().isoformat()
    _save_schedules(scheds)
    print(f"[sched] {sched_id} -> job {job_id}")


_scheduler = None


def _init_scheduler():
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        print("[sched] APScheduler no instalado")
        return
    _scheduler = BackgroundScheduler(daemon=True)
    for s in _load_schedules():
        if not s.get("enabled", True):
            continue
        try:
            _scheduler.add_job(
                _spawn_scheduled_job,
                CronTrigger.from_crontab(s["cron"]),
                id=s["id"],
                args=[s["id"]],
                replace_existing=True,
            )
        except Exception as e:
            print(f"[sched] error registrando {s.get('id')}: {e}")
    _scheduler.start()
    print(f"[sched] scheduler iniciado con {len(_scheduler.get_jobs())} jobs")


def _refresh_scheduler():
    """Re-registra todas las schedules en APScheduler."""
    if not _scheduler:
        return
    try:
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        return
    for j in list(_scheduler.get_jobs()):
        _scheduler.remove_job(j.id)
    for s in _load_schedules():
        if not s.get("enabled", True):
            continue
        try:
            _scheduler.add_job(
                _spawn_scheduled_job,
                CronTrigger.from_crontab(s["cron"]),
                id=s["id"],
                args=[s["id"]],
                replace_existing=True,
            )
        except Exception as e:
            print(f"[sched] error registrando {s.get('id')}: {e}")


_init_scheduler()


# ===== Watch folder (drop video -> auto-process) =====

WATCH_INTERVAL_SEC = int(os.environ.get("WATCH_INTERVAL", "20"))
WATCH_PROFILE = os.environ.get("WATCH_PROFILE", "").strip()
WATCH_N_CLIPS = int(os.environ.get("WATCH_N_CLIPS", "6"))
WATCH_AI = os.environ.get("WATCH_AI", "0").strip() == "1"
WATCH_INSTRUCTIONS = os.environ.get("WATCH_INSTRUCTIONS", "").strip()
WATCH_ENABLED = os.environ.get("WATCH_ENABLED", "1").strip() == "1"


def _watch_loop():
    """Polling daemon: detecta videos nuevos en watch/ y los procesa."""
    import time as _time
    seen: dict[str, int] = {}
    while True:
        try:
            _time.sleep(WATCH_INTERVAL_SEC)
            if not WATCH_DIR.exists():
                continue
            for p in WATCH_DIR.iterdir():
                if not p.is_file():
                    continue
                if p.suffix.lower() not in VIDEO_EXTS:
                    continue
                key = str(p)
                try:
                    size_now = p.stat().st_size
                except OSError:
                    continue
                if size_now == 0:
                    continue
                last_size = seen.get(key, -1)
                seen[key] = size_now
                if last_size != size_now:
                    continue  # sigue copiandose

                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                dest_name = f"watch_{ts}_{p.name}"
                dest = INPUT_DIR / dest_name
                try:
                    p.rename(dest)
                except OSError as e:
                    print(f"[watch] no pude mover {p.name}: {e}")
                    continue
                seen.pop(key, None)

                args = [str(dest), str(WATCH_N_CLIPS)]
                if WATCH_PROFILE:
                    args += ["--profile", WATCH_PROFILE]
                if WATCH_AI:
                    args += ["--ai-highlights"]
                if WATCH_INSTRUCTIONS:
                    args += ["--instructions", WATCH_INSTRUCTIONS]

                job_id = uuid.uuid4().hex[:12]
                save_job(job_id, {
                    "id": job_id,
                    "video": dest_name,
                    "url": None,
                    "use_yt": False,
                    "n_clips": WATCH_N_CLIPS,
                    "profile": WATCH_PROFILE or None,
                    "args": args,
                    "status": "queued",
                    "created": datetime.now().isoformat(),
                    "started": None,
                    "ended": None,
                    "out_dir": f"{dest.stem}_pro",
                    "from_watch": True,
                })
                threading.Thread(
                    target=run_pipeline_worker, args=(job_id,), daemon=True
                ).start()
                print(f"[watch] {p.name} -> job {job_id} "
                      f"(perfil={WATCH_PROFILE or 'default'})")
        except Exception as e:
            print(f"[watch] error en loop: {e}")


if WATCH_ENABLED:
    threading.Thread(target=_watch_loop, daemon=True).start()
    print(f"[watch] activo cada {WATCH_INTERVAL_SEC}s en {WATCH_DIR.name}/ "
          f"(N={WATCH_N_CLIPS}, perfil={WATCH_PROFILE or 'ninguno'}, "
          f"AI={'on' if WATCH_AI else 'off'})")
else:
    print("[watch] desactivado (WATCH_ENABLED=0)")


@app.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request):
    scheds = _load_schedules()
    # Calculate next run for each
    if _scheduler:
        for s in scheds:
            j = _scheduler.get_job(s["id"])
            s["next_run"] = j.next_run_time.isoformat() if j and j.next_run_time else None
    return templates.TemplateResponse(request, "schedules.html", {
        "schedules": scheds,
        "profiles": list_profiles(),
        "current_user": ADMIN_EMAIL if AUTH_ENABLED and is_authed(request) else None,
    })


@app.post("/schedules/save")
async def schedules_save(
    name: str = Form(...),
    cron: str = Form(...),
    url: str = Form(...),
    n_clips: int = Form(6),
    profile: str = Form(""),
    ai_highlights: str = Form(""),
    instructions: str = Form(""),
    sched_id: str = Form(""),
):
    # Validate cron
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(cron)
    except Exception as e:
        raise HTTPException(400, f"Cron invalido: {e}")
    if not url.startswith("http"):
        raise HTTPException(400, "URL invalida")
    scheds = _load_schedules()
    if sched_id:
        existing = next((s for s in scheds if s["id"] == sched_id), None)
        if existing:
            existing.update({
                "name": name, "cron": cron, "url": url,
                "n_clips": int(n_clips), "profile": profile,
                "ai_highlights": ai_highlights == "on",
                "instructions": instructions,
            })
    else:
        sched_id = "sched_" + uuid.uuid4().hex[:10]
        scheds.append({
            "id": sched_id, "name": name, "cron": cron, "url": url,
            "n_clips": int(n_clips), "profile": profile,
            "ai_highlights": ai_highlights == "on",
            "instructions": instructions,
            "enabled": True,
            "created": datetime.now().isoformat(),
            "last_run": None,
        })
    _save_schedules(scheds)
    _refresh_scheduler()
    return RedirectResponse("/schedules", status_code=303)


@app.post("/schedules/{sched_id}/toggle")
async def schedules_toggle(sched_id: str):
    scheds = _load_schedules()
    for s in scheds:
        if s["id"] == sched_id:
            s["enabled"] = not s.get("enabled", True)
            break
    _save_schedules(scheds)
    _refresh_scheduler()
    return RedirectResponse("/schedules", status_code=303)


@app.post("/schedules/{sched_id}/delete")
async def schedules_delete(sched_id: str):
    scheds = [s for s in _load_schedules() if s["id"] != sched_id]
    _save_schedules(scheds)
    _refresh_scheduler()
    return RedirectResponse("/schedules", status_code=303)


@app.post("/schedules/{sched_id}/run-now")
async def schedules_run_now(sched_id: str):
    _spawn_scheduled_job(sched_id)
    return RedirectResponse("/jobs", status_code=303)


@app.get("/api/stats")
async def api_stats():
    return JSONResponse(collect_stats())


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    webhook_info: dict = {"configured": False, "host": ""}
    if WEBHOOK_URL:
        from urllib.parse import urlparse
        try:
            host = urlparse(WEBHOOK_URL).netloc or "(invalid)"
        except Exception:
            host = "(?)"
        webhook_info = {"configured": True, "host": host}
    return templates.TemplateResponse(request, "stats.html", {
        "stats": collect_stats(),
        "anthropic": check_anthropic_key(),
        "webhook": webhook_info,
        "watch": {
            "enabled": WATCH_ENABLED,
            "interval": WATCH_INTERVAL_SEC,
            "profile": WATCH_PROFILE,
            "ai": WATCH_AI,
        },
        "current_user": ADMIN_EMAIL if AUTH_ENABLED and is_authed(request) else None,
    })


def search_transcripts(query: str, limit: int = 50) -> list[dict]:
    """Busca el query en todos los .txt y .segs.json de output/.
    Devuelve lista de {reel, snippet, job_id, ...} con coincidencias."""
    if not query.strip() or not OUTPUT_DIR.exists():
        return []
    q = query.lower().strip()
    matches = []
    # Map out_dir -> job_id for clickable links
    out_to_job: dict[str, str] = {}
    if JOBS_DIR.exists():
        for jp in JOBS_DIR.glob("*.json"):
            try:
                d = json.loads(jp.read_text(encoding="utf-8"))
                out_to_job[d.get("out_dir", "")] = d.get("id", "")
            except Exception:
                continue

    for txt_path in OUTPUT_DIR.rglob("reel_*.txt"):
        try:
            content = txt_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lower = content.lower()
        if q not in lower:
            continue
        # Snippet con contexto (~120 chars alrededor de la primera coincidencia)
        idx = lower.index(q)
        start = max(0, idx - 60)
        end = min(len(content), idx + len(q) + 60)
        snippet = content[start:end].replace("\n", " ").strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."
        out_dir_name = txt_path.parent.name
        reel_stem = txt_path.stem
        mp4 = txt_path.with_suffix(".mp4")
        thumb = txt_path.with_suffix(".jpg")
        try:
            reel_id = int(reel_stem.replace("reel_", ""))
        except ValueError:
            reel_id = None
        matches.append({
            "out_dir": out_dir_name,
            "reel_name": mp4.name,
            "reel_id": reel_id,
            "video": f"/output/{out_dir_name}/{mp4.name}" if mp4.exists() else None,
            "thumb": f"/output/{out_dir_name}/{thumb.name}" if thumb.exists() else None,
            "job_id": out_to_job.get(out_dir_name, ""),
            "snippet": snippet,
            "mtime": txt_path.stat().st_mtime,
        })
    matches.sort(key=lambda m: m["mtime"], reverse=True)
    return matches[:limit]


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = ""):
    results = search_transcripts(q) if q else []
    return templates.TemplateResponse(request, "search.html", {
        "q": q,
        "results": results,
        "current_user": ADMIN_EMAIL if AUTH_ENABLED and is_authed(request) else None,
    })


def find_recent_reels(limit: int = 6) -> list[dict]:
    """Devuelve los ultimos N reels generados (por mtime) con miniaturas."""
    if not OUTPUT_DIR.exists():
        return []
    reels = []
    for mp4 in OUTPUT_DIR.rglob("reel_*.mp4"):
        if not mp4.is_file():
            continue
        try:
            mtime = mp4.stat().st_mtime
        except OSError:
            continue
        out_dir_name = mp4.parent.name
        # Find the job that owns this output dir
        job_id = None
        if JOBS_DIR.exists():
            for jp in JOBS_DIR.glob("*.json"):
                try:
                    d = json.loads(jp.read_text(encoding="utf-8"))
                    if d.get("out_dir") == out_dir_name:
                        job_id = d["id"]
                        break
                except Exception:
                    continue
        thumb = mp4.with_suffix(".jpg")
        reels.append({
            "name": mp4.name,
            "video": f"/output/{out_dir_name}/{mp4.name}",
            "thumb": f"/output/{out_dir_name}/{thumb.name}" if thumb.exists() else None,
            "size_mb": round(mp4.stat().st_size / (1024 * 1024), 1),
            "mtime": mtime,
            "job_id": job_id,
            "out_dir": out_dir_name,
        })
    reels.sort(key=lambda r: r["mtime"], reverse=True)
    return reels[:limit]


@app.post("/jobs/bulk-delete")
async def jobs_bulk_delete(
    job_ids: str = Form(...),
    delete_outputs: str = Form("on"),
):
    """Borra una lista de jobs por id (separados por coma) y opcionalmente sus outputs."""
    ids = [j.strip() for j in job_ids.split(",") if j.strip()]
    if not ids:
        return RedirectResponse("/jobs", status_code=303)
    deleted = 0
    freed_bytes = 0
    for jid in ids:
        jp = JOBS_DIR / f"{jid}.json"
        if not jp.exists():
            continue
        try:
            d = json.loads(jp.read_text(encoding="utf-8"))
            if delete_outputs == "on":
                out_dir = OUTPUT_DIR / d.get("out_dir", "")
                if out_dir.exists():
                    freed_bytes += dir_size(out_dir)
                    shutil.rmtree(out_dir, ignore_errors=True)
                montage = OUTPUT_DIR / f"{d.get('out_dir','')}_montage.mp4"
                if montage.exists():
                    freed_bytes += montage.stat().st_size
                    montage.unlink(missing_ok=True)
            log_file = JOBS_DIR / f"{d['id']}.log"
            log_file.unlink(missing_ok=True)
            jp.unlink(missing_ok=True)
            deleted += 1
        except Exception:
            continue
    print(f"[bulk-delete] {deleted} jobs, {freed_bytes/(1024*1024):.1f} MB liberados")
    return RedirectResponse("/jobs", status_code=303)


@app.post("/jobs/cleanup")
async def jobs_cleanup(
    older_than_days: int = Form(30),
    only_failed: str = Form(""),
    delete_outputs: str = Form("on"),
):
    """Borra metadata de jobs viejos y opcionalmente sus outputs."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=int(older_than_days))).timestamp()
    deleted_jobs = 0
    deleted_dirs = 0
    freed_bytes = 0
    if not JOBS_DIR.exists():
        return RedirectResponse("/stats", status_code=303)
    for jp in list(JOBS_DIR.glob("*.json")):
        try:
            if jp.stat().st_mtime > cutoff:
                continue
            d = json.loads(jp.read_text(encoding="utf-8"))
            if only_failed == "on" and d.get("status") not in (
                    "error", "cancelled", "interrupted"):
                continue
            if delete_outputs == "on":
                out_dir = OUTPUT_DIR / d.get("out_dir", "")
                if out_dir.exists():
                    freed_bytes += dir_size(out_dir)
                    shutil.rmtree(out_dir, ignore_errors=True)
                    deleted_dirs += 1
                montage = OUTPUT_DIR / f"{d.get('out_dir','')}_montage.mp4"
                if montage.exists():
                    freed_bytes += montage.stat().st_size
                    montage.unlink(missing_ok=True)
            log_file = JOBS_DIR / f"{d['id']}.log"
            log_file.unlink(missing_ok=True)
            jp.unlink(missing_ok=True)
            deleted_jobs += 1
        except Exception:
            continue
    print(f"[cleanup] {deleted_jobs} jobs, {deleted_dirs} dirs, "
          f"{freed_bytes/(1024*1024):.1f} MB liberados")
    return RedirectResponse("/stats", status_code=303)
