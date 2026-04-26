"""
Comprime y optimiza videos para web (H.264 + faststart, max 1920px ancho).
Procesa en batch toda la carpeta input/.

Uso:
    python scripts/04_comprimir_web.py
    python scripts/04_comprimir_web.py --crf 26

Parametros:
    --crf  18 (alta calidad) - 23 (default) - 28 (peso minimo)
"""
import subprocess
import sys
from pathlib import Path

EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}


def comprimir(video: Path, output: Path, crf: int) -> None:
    cmd = ["ffmpeg", "-y", "-i", str(video),
           "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
           "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart",
           "-vf", "scale='min(1920,iw)':-2",
           str(output)]
    subprocess.run(cmd, check=True, capture_output=True)


def main(crf: int = 23) -> None:
    input_dir = Path("input")
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    videos = sorted(v for v in input_dir.iterdir() if v.suffix.lower() in EXTS)
    if not videos:
        print(f"[!!] No hay videos en {input_dir}/")
        return

    print(f"[..] Procesando {len(videos)} videos (CRF {crf})")
    total_in = total_out = 0.0
    for i, v in enumerate(videos, 1):
        out = output_dir / f"{v.stem}_web.mp4"
        size_in = v.stat().st_size / (1024 * 1024)
        print(f"[..] [{i}/{len(videos)}] {v.name} ({size_in:.1f} MB)")
        comprimir(v, out, crf)
        size_out = out.stat().st_size / (1024 * 1024)
        ahorro = (1 - size_out / size_in) * 100
        print(f"[OK] {out.name} ({size_out:.1f} MB) -- ahorro {ahorro:.0f}%")
        total_in += size_in
        total_out += size_out

    print(f"\n[OK] Total: {total_in:.1f} MB -> {total_out:.1f} MB "
          f"({(1 - total_out/total_in)*100:.0f}% reducido)")


if __name__ == "__main__":
    crf_val = 23
    if "--crf" in sys.argv:
        crf_val = int(sys.argv[sys.argv.index("--crf") + 1])
    main(crf_val)
