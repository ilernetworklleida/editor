"""
Detecta silencios y los elimina del video.
Util para limpiar podcasts, tutoriales, voz en off.

Uso:
    python scripts/03_cortar_silencios.py input/video.mp4
    python scripts/03_cortar_silencios.py input/video.mp4 --umbral -30 --min 0.7

Parametros:
    --umbral  dB por debajo de los cuales se considera silencio (default -30)
    --min     duracion minima en segundos para cortar (default 0.7)
"""
import re
import subprocess
import sys
from pathlib import Path


def detectar_silencios(video: Path, umbral_db: int, min_seg: float) -> list[dict]:
    """Usa ffmpeg silencedetect y devuelve lista de tramos silenciosos."""
    cmd = ["ffmpeg", "-i", str(video), "-af",
           f"silencedetect=noise={umbral_db}dB:d={min_seg}", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    log = out.stderr

    starts = [float(m.group(1)) for m in re.finditer(r"silence_start: ([\d.]+)", log)]
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end: ([\d.]+)", log)]
    return [{"start": s, "end": e} for s, e in zip(starts, ends)]


def get_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def main(video_path: str, umbral_db: int = -30, min_seg: float = 0.7) -> None:
    video = Path(video_path)
    if not video.exists():
        print(f"[ERROR] No existe: {video}")
        sys.exit(1)

    print(f"[..] Detectando silencios (umbral {umbral_db}dB, min {min_seg}s)")
    silencios = detectar_silencios(video, umbral_db, min_seg)
    print(f"[..] Encontrados {len(silencios)} silencios")
    if not silencios:
        print("[!!] Nada que cortar")
        return

    duration = get_duration(video)

    # Tramos a CONSERVAR = los huecos entre silencios
    tramos = []
    cursor = 0.0
    for s in silencios:
        if s["start"] > cursor:
            tramos.append((cursor, s["start"]))
        cursor = s["end"]
    if cursor < duration:
        tramos.append((cursor, duration))

    # Filtro complejo: trim de cada tramo + concat
    fv = [f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}]"
          for i, (a, b) in enumerate(tramos)]
    fa = [f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}]"
          for i, (a, b) in enumerate(tramos)]
    concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(tramos)))
    filtro = ";".join(fv + fa) + f";{concat_in}concat=n={len(tramos)}:v=1:a=1[v][a]"

    out_path = Path("output") / f"{video.stem}_sin_silencios.mp4"
    out_path.parent.mkdir(exist_ok=True)

    cmd = ["ffmpeg", "-y", "-i", str(video), "-filter_complex", filtro,
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "fast",
           "-crf", "23", "-c:a", "aac", str(out_path)]

    print(f"[..] Generando video limpio ({len(tramos)} tramos)")
    subprocess.run(cmd, check=True)

    new_duration = get_duration(out_path)
    ahorro = (1 - new_duration / duration) * 100
    print(f"[OK] {out_path}")
    print(f"[OK] Duracion: {duration:.1f}s -> {new_duration:.1f}s ({ahorro:.0f}% mas corto)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    args = sys.argv[2:]
    umbral = int(args[args.index("--umbral") + 1]) if "--umbral" in args else -30
    min_s = float(args[args.index("--min") + 1]) if "--min" in args else 0.7
    main(sys.argv[1], umbral, min_s)
