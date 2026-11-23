# LUNAR — Backend + Modeling

API FastAPI + model pengenalan isyarat BISINDO (`ml/`, ONNX) untuk **LUNAR**, platform belajar inklusif untuk siswa Tuli. Frontend berada di repo terpisah.

## Isi Repo

| Folder / File | Keterangan |
|---|---|
| `api/index.py` | Entrypoint Vercel Serverless untuk aplikasi FastAPI |
| `backend/` | API FastAPI (Python 3.12), auth Supabase, repository Postgres, test, dan CLI operator (`backend/scripts/`) |
| `ml/` | Paket `bisindo`: model ONNX (`artifacts/`), model MediaPipe (`mp_models/`), dan skrip training |
| `Database/migrations/` | Migrasi SQL Supabase (urut timestamp, idempoten) |
| `BACKEND.md` | **Dokumentasi lengkap Backend** (arsitektur, cara menjalankan lokal, testing, script CLI, dan deploy) |
| `AGENTS.md`, `decisions.md`, `PRD.md`, `SYSTEM_DESIGN.md`, `ARCHITECTURE.md` | Aturan engineering, ADR, spesifikasi produk, desain sistem, dan arsitektur |

## Menjalankan Secara Lokal

Panduan lengkap instalasi virtual environment, konfigurasi `.env`, menjalankan server di macOS/Linux/Windows, hingga `pytest` tersedia di **[BACKEND.md](BACKEND.md)**.

Ringkasan cepat (macOS / Linux):
```bash
cd backend
set -a && source .env && set +a
.venv/bin/python serve.py --host 127.0.0.1 --port 8000
```

Cek status server: `GET http://127.0.0.1:8000/api/health`.

> `backend/.env` berisi kredensial rahasia dan sudah di-ignore oleh Git serta Vercel. Jangan pernah di-commit.
