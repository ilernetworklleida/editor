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

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}

for _d in [INPUT_DIR, OUTPUT_DIR, JOBS_DIR, PROFILES_DIR, MUSIC_DIR, BRANDING_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

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
    """Protege rutas. Login form en /login. Static y health publicos."""

    async def dispatch(self, request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)
        path = request.url.path
        if (path in {"/login", "/api/health"}
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
    """Extrae el ID de una URL de YouTube. None si no es URL valida."""
    m = re.search(
        r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{6,15})", url
    )
    return m.group(1) if m else None


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


def collect_stats() -> dict:
    """Estadisticas globales de uso del proyecto."""
    counts = {"queued": 0, "running": 0, "done": 0, "error": 0,
              "cancelled": 0, "interrupted": 0}
    total_jobs = 0
    if JOBS_DIR.exists():
        for p in JOBS_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                s = d.get("status", "unknown")
                counts[s] = counts.get(s, 0) + 1
                total_jobs += 1
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
        save_job(job_id, job)
    except Exception as e:
        job = load_job(job_id) or job
        job["status"] = "error"
        job["error"] = str(e)
        job["ended"] = datetime.now().isoformat()
        job.pop("pid", None)
        save_job(job_id, job)


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
    return templates.TemplateResponse(request, "home.html", {
        "videos": list_videos(),
        "profiles": list_profiles(),
        "music_files": _list_dir(MUSIC_DIR, MUSIC_EXTS),
        "watermarks": _list_dir(BRANDING_DIR, IMG_EXTS),
        "recent_jobs": recent,
        "current_user": ADMIN_EMAIL if AUTH_ENABLED and is_authed(request) else None,
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
):
    url = url.strip()
    if not video and not url:
        raise HTTPException(400, "Tienes que especificar un video O una URL")

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


@app.get("/api/stats")
async def api_stats():
    return JSONResponse(collect_stats())


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    return templates.TemplateResponse(request, "stats.html", {
        "stats": collect_stats(),
        "current_user": ADMIN_EMAIL if AUTH_ENABLED and is_authed(request) else None,
    })


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
