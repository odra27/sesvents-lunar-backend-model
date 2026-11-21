# LUNAR — Backend + Modeling

API FastAPI + model pengenalan isyarat BISINDO (`ml/`, ONNX) untuk LUNAR, platform belajar inklusif untuk siswa Tuli. Frontend ada di repo terpisah (`Sesvents-Frontend`).

## Isi repo

| Folder / file | Isi |
|---|---|
| `backend/` | API FastAPI (Python 3.12), test, skrip admin (`scripts/`) |
| `ml/` | Paket `bisindo`: model, artefak aktif (`artifacts/`), model MediaPipe (`mp_models/`), skrip training |
| `Database/` | Migration SQL Supabase (urut timestamp) |
| `Dockerfile` | Satu image untuk deploy: API + model dalam satu service |
| `start-dev.ps1` / `stop-dev.ps1` | Jalankan / matikan backend lokal (`.cmd` = versi klik dua kali) |
| `AGENTS.md`, `decisions.md`, `PRD.md`, `SYSTEM_DESIGN.md`, `ARCHITECTURE.md` | Aturan kerja, ADR, produk, kontrak API, arsitektur |

## Jalankan lokal (Windows)

```powershell
# 1. Isi backend\.env dari backend\.env.example (kosong = DB in-memory, tanpa login/chat)
# 2. Jalankan (setup venv Python 3.12 otomatis saat pertama kali)
.\start-dev.ps1
```

API di `http://127.0.0.1:8000` (cek `GET /api/health`; model dimuat ~30 detik). Matikan dengan `.\stop-dev.ps1`.

Test: dari `backend\`, `.\.venv\Scripts\python.exe -m pytest`.

## Deploy

```bash
docker build -t lunar-backend .
docker run -p 8000:8000 --env-file backend/.env lunar-backend
```

Di Railway / Render cukup hubungkan repo ini; `Dockerfile` di root terdeteksi otomatis. Isi variabel dari `backend/.env.example` di dashboard (jangan set `LOCAL_DEV_DB`) dan set `CORS_ALLOW_ORIGINS` ke URL frontend. Detail: `ARCHITECTURE.md` §9.

> `backend/.env` berisi rahasia dan sudah di-ignore git. Jangan di-commit.
