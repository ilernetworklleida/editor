"""
auto_yt.py — Pipeline desde URL de YouTube en un solo comando.

Baja el video con yt-dlp (cache en input/yt_<ID>.mp4 para no re-bajar)
y lo procesa con auto_reels_pro.

Uso:
    python scripts/auto_yt.py "https://youtube.com/watch?v=ID" 6
    python scripts/auto_yt.py "https://youtube.com/watch?v=ID" 6 --range 60 600
    python scripts/auto_yt.py "https://youtube.com/watch?v=ID" 6 --equal
    python scripts/auto_yt.py "https://youtube.com/watch?v=ID" 6 --duration 30 --chunk 4

Argumentos:
    url        URL completa de YouTube (entre comillas).
    n_clips    cuantos reels generar.
    --range S E  baja solo desde S hasta E segundos (test rapido).
    --equal      pasa al pipeline modo equal en vez de smart highlights.
    --duration N  duracion ideal por clip en segundos (default 35).
    --chunk N    palabras por bocadillo de subtitulo (default 3).
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path


def find_yt_dlp() -> str:
    p = shutil.which("yt-dlp")
    if p:
        return p
    base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if base.exists():
        cands = list(base.rglob("yt-dlp.exe"))
        if cands:
            return str(cands[0])
    return "yt-dlp"


def find_ffmpeg_dir() -> str | None:
    p = shutil.which("ffmpeg")
    if p:
        return str(Path(p).parent)
    base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if base.exists():
        for c in base.rglob("ffmpeg.exe"):
            if "Gyan.FFmpeg" in str(c):
                return str(c.parent)
        cands = list(base.rglob("ffmpeg.exe"))
        if cands:
            return str(cands[0].parent)
    return None


def extract_video_id(url: str) -> str:
    """Extrae un ID estable de cualquier URL soportada por yt-dlp.
    YouTube usa el v=ID. Otros (TikTok/IG/Twitter/Vimeo/Twitch...) usan
    un hash corto del URL para tener cache estable."""
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{6,15})", url)
    if m:
        return m.group(1)
    import hashlib
    return "u" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:11]


def download(url: str, out_path: Path,
             range_start: int | None = None,
             range_end: int | None = None) -> None:
    yt = find_yt_dlp()
    ffdir = find_ffmpeg_dir()
    cmd = [yt]
    if ffdir:
        cmd += ["--ffmpeg-location", ffdir]
    cmd += [
        "-f", "bestvideo[height<=1080]+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        "--force-overwrites",
    ]
    if range_start is not None and range_end is not None:
        cmd += ["--download-sections", f"*{range_start}-{range_end}"]
    cmd.append(url)
    print(f"[..] Bajando con yt-dlp ({yt})")
    res = subprocess.run(cmd)
    if res.returncode != 0:
        print(f"[ERROR] Fallo la descarga (codigo {res.returncode})")
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    url = args[0]
    try:
        n_clips = int(args[1])
    except ValueError:
        print(f"[ERROR] Segundo argumento debe ser un numero (n_clips), no '{args[1]}'")
        sys.exit(1)

    rest = args[2:]
    range_start = range_end = None
    if "--range" in rest:
        idx = rest.index("--range")
        try:
            range_start = int(rest[idx + 1])
            range_end = int(rest[idx + 2])
        except (IndexError, ValueError):
            print("[ERROR] --range requiere DOS numeros: --range INICIO FIN")
            sys.exit(1)
        del rest[idx:idx + 3]

    video_id = extract_video_id(url)
    download_dir = Path("input")
    download_dir.mkdir(exist_ok=True)
    video_path = download_dir / f"yt_{video_id}.mp4"

    if video_path.exists():
        print(f"[OK] Ya existe {video_path}, no re-descargo")
        print(f"     (borralo manualmente si quieres re-bajar)")
    else:
        download(url, video_path, range_start, range_end)
        if not video_path.exists():
            print(f"[ERROR] El video no se guardo en {video_path}")
            sys.exit(1)

    print(f"\n[..] Procesando con auto_reels_pro: {video_path.name}")
    here = Path(__file__).resolve().parent
    cmd = [
        sys.executable,
        str(here / "auto_reels_pro.py"),
        str(video_path),
        str(n_clips),
    ] + rest
    res = subprocess.run(cmd)
    sys.exit(res.returncode)


if __name__ == "__main__":
    main()
