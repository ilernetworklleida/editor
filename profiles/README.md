# Perfiles

Cada `.json` aqui es un conjunto de flags reutilizable para `auto_reels_pro.py`.

## Uso

```bash
python scripts/auto_reels_pro.py input/v.mp4 6 --profile viral
python scripts/auto_reels_pro.py input/v.mp4 6 --profile educativo --music music/cancion.mp3
```

Los flags que pases por linea de comandos **anulan** los del perfil:
```bash
# El perfil viral usa --grade vivid, pero forzamos warm:
python scripts/auto_reels_pro.py input/v.mp4 6 --profile viral --grade warm
```

## Crear tu propio perfil

Copia uno existente, edita los flags y guardalo como `mi_canal.json`:

```json
[
    "--style", "hype",
    "--grade", "cinematic",
    "--chunk", "2",
    "--duration", "28",
    "--outro", "@mi_canal\nSigueme para mas",
    "--outro-duration", "2.0"
]
```

Despues:

```bash
python scripts/auto_reels_pro.py input/v.mp4 6 --profile mi_canal
```

## Perfiles incluidos

- `viral.json`: hype + vivid + chunks de 2 + 25s. Para Reels/Shorts/TikTok.
- `educativo.json`: clean + warm + chunks de 3 + 40s. Tutoriales, explicaciones.
- `finanzas.json`: money + cold + chunks de 3 + 30s. Trading, business.
