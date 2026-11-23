# Dokumentasi Backend & Modeling — LUNAR

Dokumentasi ini menjelaskan arsitektur, struktur folder, alur autentikasi & database, cara menjalankan server secara lokal (macOS/Linux maupun Windows), cara menjalankan pengujian, serta cara mengelola migrasi dan deployment Vercel untuk backend **LUNAR**.

---

## 1. Gambaran Umum Arsitektur

Backend **LUNAR** dibangun dengan **Python 3.12 + FastAPI** dan terhubung ke **Supabase** (PostgreSQL, Auth/JWT, Storage) serta model pengenalan isyarat BISINDO berbasis **ONNX Runtime** (`ml/`).

Alur utama yang ditangani backend:
1. **Autentikasi & Profil Siswa (Email-only, B2C):**
   - Login (`POST /api/student/auth/login`), daftar mandiri (`POST /api/student/auth/register` dengan `full_name`, `email`, `phone` awalan `0`, `password`), lupa password (`POST /api/student/auth/forgot-password`), refresh token, dan lengkapi profil (`PATCH /api/student/me`).
   - Pelacakan sesi login otomatis ke tabel `user_login_sessions` lengkap dengan `visitor_id` anonim, heartbeat, logout, dan ringkasan waktu WIB.
2. **Chatbot Tutor Inklusif:**
   - Manajemen sesi chat (`/api/chat/sessions`), percakapan dengan konteks video pelajaran & ekstraksi teks PDF (`/api/chat/sessions/{id}/documents`), serta ringkasan aktivitas belajar (`/api/chat/activity`).
3. **Pengenalan Isyarat BISINDO (`Sign → Text`):**
   - `POST /api/match/sign` memvalidasi sequence landmark, memanggil `Predictor` ONNX (`ml/src/bisindo`), lalu menerapkan threshold keputusan bisnis di backend (`accepted` ≥ 0.85, `confirmed` 0.60–0.84, `escalated_to_jbi` < 0.60).
4. **Staff / Admin & Kiosk:**
   - Login staf/admin (`POST /api/auth/login`), manajemen kosakata & video referensi (`/api/admin/*`), serta endpoint kiosk (`/api/devices/*`, `/api/phrases`, `/api/logs`).

---

## 2. Struktur Folder Penting

```text
.
├── api/
│   └── index.py              # Entrypoint Vercel Serverless (membungkus FastAPI app)
├── backend/
│   ├── app/
│   │   ├── api/              # Router FastAPI (student, chat, auth, admin, sign, phrases, logs, kiosk, health)
│   │   ├── auth/             # Verifikasi JWT Supabase (JWKS), dependency require_student & require_staff
│   │   ├── chat/             # Klien OpenRouter / offline fallback & ekstraktor PDF
│   │   ├── db/               # Repository PostgreSQL (psycopg 3, parameterized SQL) & local dev DB
│   │   ├── learning/         # Katalog materi video pelajaran
│   │   ├── matching/         # Business decision logic (confidence thresholds)
│   │   ├── modeling/         # Boundary wrapper memanggil ml/ Predictor + timeout handling
│   │   ├── provisioning/     # Klien Supabase Admin API (create/delete user + rollback)
│   │   ├── storage/          # Klien Supabase Storage (upload & signed URL video referensi)
│   │   ├── errors.py         # Domain exception classes
│   │   └── main.py           # App factory (create_app), middleware CORS, error handlers
│   ├── scripts/              # CLI operator (apply_migrations.py, create_student.py, track_usage.py)
│   ├── tests/                # Suite pengujian unit, kontrak API, dan integrasi Postgres
│   ├── pyproject.toml        # Dependency & konfigurasi pytest
│   └── serve.py              # Launcher uvicorn lokal (mengatur SelectorEventLoop untuk Windows)
├── Database/
│   └── migrations/           # File migrasi SQL berurutan (idempoten, non-destruktif)
├── ml/                       # Paket bisindo, model ONNX (artifacts/), dan model MediaPipe
├── requirements.txt          # Dependency runtime ramping untuk Vercel Serverless (< 250 MB)
├── vercel.json               # Konfigurasi routing Vercel ke api/index.py
└── .vercelignore             # Mengecualikan file non-runtime (tests, training data, .env, dll.)
```

---

## 3. Cara Menjalankan di Lokal (Tanpa `scripts/dev/`)

> **Prasyarat:** Python **3.12** terpasang di sistem.

### A. Persiapan Awal (Sekali Saja)

1. **Buat dan aktifkan virtual environment di `backend/.venv`:**

   **macOS / Linux:**
   ```bash
   cd backend
   python3.12 -m venv .venv
   source .venv/bin/activate
   ```

   **Windows (PowerShell):**
   ```powershell
   cd backend
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. **Install dependency backend + paket modeling (`ml/`):**
   ```bash
   pip install --upgrade pip
   pip install -e ".[test]"
   pip install -e ../ml
   ```

3. **Siapkan file environment `backend/.env`:**
   Salin dari `backend/.env.example` lalu isi variabel Supabase & OpenRouter:
   ```bash
   cp .env.example .env
   ```
   Variabel penting di `backend/.env`:
   - `DATABASE_URL`: connection string PostgreSQL Supabase (`postgresql://...`)
   - `SUPABASE_URL`: URL project Supabase (`https://<ref>.supabase.co`)
   - `SUPABASE_ANON_KEY` & `SUPABASE_SERVICE_ROLE_KEY`: key dari dashboard Supabase
   - `OPENROUTER_API_KEY`: opsional; jika kosong chatbot berjalan dalam mode offline
   - `CORS_ALLOW_ORIGINS`: origin frontend (mis. `http://localhost:5174,https://lunarsesvent.vercel.app`)

   *(Jika `DATABASE_URL` dikosongkan dan `LOCAL_DEV_DB=1`, server memakai database in-memory khusus dev tanpa login/chat Supabase.)*

---

### B. Menjalankan Server Backend Lokal

Dari folder `backend/` (pastikan `.venv` sudah aktif atau panggil python dari `.venv`):

**macOS / Linux:**
```bash
cd backend
set -a && source .env && set +a
.venv/bin/python serve.py --host 127.0.0.1 --port 8000
```
Atau langsung memakai `uvicorn` dengan auto-reload saat coding:
```bash
cd backend
set -a && source .env && set +a
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

**Windows (PowerShell):**
```powershell
cd backend
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
    [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process')
  }
}
.\.venv\Scripts\python.exe serve.py --host 127.0.0.1 --port 8000
```
> **Catatan Windows:** Gunakan `serve.py` (bukan `uvicorn` langsung tanpa `--reload`) karena `psycopg` async membutuhkan `SelectorEventLoop`, yang sudah diatur otomatis oleh `serve.py`.

Server akan berjalan di **`http://127.0.0.1:8000`**.
Cek status kesehatan server di browser atau terminal:
```bash
curl http://127.0.0.1:8000/api/health
```
Dokumentasi interaktif OpenAPI tersedia di `http://127.0.0.1:8000/docs`. Tekan `Ctrl + C` di terminal untuk mematikan server.

---

## 4. Menjalankan Pengujian (`pytest`)

Dari root repo:
```bash
backend/.venv/bin/pytest backend/tests/ -q
```
Atau dari dalam folder `backend/`:
```bash
.venv/bin/pytest tests/test_student_auth.py tests/test_login_sessions.py -q
```

---

## 5. Script Operator CLI (`backend/scripts/`)

1. **Cek & terapkan migrasi SQL ke Supabase:**
   ```bash
   backend/.venv/bin/python backend/scripts/apply_migrations.py          # cek status
   backend/.venv/bin/python backend/scripts/apply_migrations.py --apply  # terapkan migrasi baru
   ```
2. **Lihat laporan aktivitas login, visitor ID, dan durasi sesi:**
   ```bash
   backend/.venv/bin/python backend/scripts/track_usage.py --days 30 --tz Asia/Jakarta
   ```
3. **Buat akun siswa/demo/juri manual:**
   ```bash
   backend/.venv/bin/python backend/scripts/create_student.py --email nama@gmail.com --name "Nama Siswa" --grade 5
   ```

---

## 6. Deployment (Vercel Serverless)

Project ini sudah dikonfigurasi untuk deploy langsung ke **Vercel** melalui `api/index.py`, `vercel.json`, `requirements.txt`, `.python-version` (`3.12`), dan `.vercelignore`:
- Push ke branch `main` di GitHub repo deployment otomatis memicu build & deploy di Vercel.
- Pastikan seluruh Environment Variables (`DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `CORS_ALLOW_ORIGINS`, `OPENROUTER_API_KEY`) sudah diisi di **Vercel Dashboard → Settings → Environment Variables**.
- File `backend/.env` selalu di-ignore oleh `.gitignore` dan `.vercelignore` — **jangan pernah meng-commit `.env` ke GitHub**.
