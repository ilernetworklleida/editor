"""
app/main.py — FastAPI web UI para auto_reels_pro.

Sirve una interfaz web completa para subir un video, configurar el pipeline
y ver los reels resultantes. Lanza con:

    python scripts/run_web.py

Auth: si defines EDITOR_USER y EDITOR_PASS (variables de entorno o .env),
todas las rutas requieren HTTP Basic Auth. Sin definirlas: modo local sin auth.
"""
from __future__ import annotations

import base64
import json
import os
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


# ===== HTTP Basic Auth (opcional, via env vars EDITOR_USER + EDITOR_PASS) =====

class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Protege TODAS las rutas (incluyendo mounts de StaticFiles) con HTTP Basic.
    Solo activo si EDITOR_USER y EDITOR_PASS estan definidas. Excluye paths
    publicos como /api/health para health-checks externos."""

    def __init__(self, app, user: str, pwd: str, exempt: set[str] | None = None):
        super().__init__(app)
        self.user = user
        self.pwd = pwd
        self.exempt = exempt or set()

    async def dispatch(self, request, call_next):
        if request.url.path in self.exempt:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return self._unauthorized()
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            user, _, pwd = decoded.partition(":")
        except Exception:
            return self._unauthorized()
        ok_user = secrets.compare_digest(user, self.user)
        ok_pwd = secrets.compare_digest(pwd, self.pwd)
        if not (ok_user and ok_pwd):
            return self._unauthorized()
        return await call_next(request)

    @staticmethod
    def _unauthorized() -> Response:
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Editor"'},
        )


_AUTH_USER = os.environ.get("EDITOR_USER", "").strip()
_AUTH_PASS = os.environ.get("EDITOR_PASS", "").strip()
if _AUTH_USER and _AUTH_PASS:
    app.add_middleware(
        BasicAuthMiddleware,
        user=_AUTH_USER,
        pwd=_AUTH_PASS,
        exempt={"/api/health"},
    )
    print(f"[auth] Basic Auth activa (user='{_AUTH_USER}')")
else:
    print("[auth] Sin auth (define EDITOR_USER y EDITOR_PASS para protegerlo)")


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


def list_jobs(limit: int = 12) -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    files = sorted(
        JOBS_DIR.glob("*.json"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    out = []
    for p in files[:limit]:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return out


def find_reels(out_dir_name: str) -> list[dict]:
    out_dir = OUTPUT_DIR / out_dir_name
    if not out_dir.exists():
        return []
    reels = []
    for mp4 in sorted(out_dir.glob("reel_*.mp4")):
        stem = mp4.stem
        thumb = out_dir / f"{stem}.jpg"
        txt = out_dir / f"{stem}.txt"
        info: dict = {"name": mp4.name, "stem": stem,
                      "video": f"/output/{out_dir_name}/{mp4.name}"}
        if thumb.exists():
            info["thumb"] = f"/output/{out_dir_name}/{thumb.name}"
        if txt.exists():
            try:
                info["txt"] = txt.read_text(encoding="utf-8")
            except Exception:
                info["txt"] = ""
        info["size_mb"] = round(mp4.stat().st_size / (1024 * 1024), 1)
        reels.append(info)
    return reels


# ===== Pipeline runner =====

def run_pipeline_worker(job_id: str) -> None:
    job = load_job(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started"] = datetime.now().isoformat()
    save_job(job_id, job)

    cmd = [sys.executable, str(SCRIPTS_DIR / "auto_reels_pro.py")] + list(job["args"])
    log_path = JOBS_DIR / f"{job_id}.log"

    try:
        with log_path.open("w", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
            rc = proc.wait()
        job = load_job(job_id) or job
        job["status"] = "done" if rc == 0 else "error"
        job["return_code"] = rc
        job["ended"] = datetime.now().isoformat()
        save_job(job_id, job)
    except Exception as e:
        job = load_job(job_id) or job
        job["status"] = "error"
        job["error"] = str(e)
        job["ended"] = datetime.now().isoformat()
        save_job(job_id, job)


# ===== Routes =====

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {
        "videos": list_videos(),
        "profiles": list_profiles(),
        "music_files": _list_dir(MUSIC_DIR, MUSIC_EXTS),
        "watermarks": _list_dir(BRANDING_DIR, IMG_EXTS),
        "recent_jobs": list_jobs(limit=8),
    })


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
    video: str = Form(...),
    n_clips: int = Form(6),
    profile: str = Form(""),
    style: str = Form("clean"),
    grade: str = Form("none"),
    duration: float = Form(35),
    chunk: int = Form(3),
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
):
    video_path = INPUT_DIR / video
    if not video_path.exists():
        raise HTTPException(404, f"Video no existe: {video}")

    args: list[str] = [str(video_path), str(int(n_clips))]
    if profile:
        args += ["--profile", profile]
    else:
        if style and style != "clean":
            args += ["--style", style]
        if grade and grade != "none":
            args += ["--grade", grade]
        if duration and float(duration) != 35.0:
            args += ["--duration", str(duration)]
        if chunk and int(chunk) != 3:
            args += ["--chunk", str(int(chunk))]
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

    job_id = uuid.uuid4().hex[:12]
    out_dir_name = f"{video_path.stem}_pro"
    job_data = {
        "id": job_id,
        "video": video,
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
    return templates.TemplateResponse(request, "job.html", {
        "job": job,
        "reels": reels,
    })


@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(404)
    log_path = JOBS_DIR / f"{job_id}.log"
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    reels = find_reels(job.get("out_dir", "")) if job.get("status") == "done" else []
    return JSONResponse({
        "id": job["id"],
        "status": job["status"],
        "log": log[-4000:],
        "ended": job.get("ended"),
        "reels": reels,
    })


@app.get("/api/health")
async def health():
    return {"ok": True, "version": "1.0"}
