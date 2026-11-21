"""Laporan tracking penggunaan akun siswa / demo (juri & coba) — ADR-054.

Menampilkan:
  - Seberapa sering akun di-login (`login` dan `token_refreshed` di `auth.audit_log_entries`)
  - Kapan terakhir dipakai login (`last_sign_in_at` di `auth.users`)
  - Jam sibuk kunjungan (Peak Hours 00:00–23:00 dalam timezone pilihan)
  - Distribusi kunjungan per hari

Contoh penggunaan:
    cd backend && set -a && source .env && set +a
    .venv/bin/python scripts/track_usage.py --nis 1001 1002 --days 30 --tz Asia/Jakarta
    .venv/bin/python scripts/track_usage.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.api.admin import DEFAULT_USAGE_DAYS, DEFAULT_USAGE_TZ, parse_nis_values  # noqa: E402
from app.db.admin import PostgresAdminRepository  # noqa: E402
from scripts.apply_migrations import _load_env_file  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Tracking aktivitas login & sesi akun siswa/demo (ADR-054)."
    )
    p.add_argument(
        "--nis",
        nargs="*",
        help="Daftar NIS akun yang dilacak (mis. --nis 1001 1002). Kosong = TRACKED_STUDENT_NIS atau semua akun.",
    )
    p.add_argument(
        "--days",
        type=int,
        default=DEFAULT_USAGE_DAYS,
        help=f"Rentang hari ke belakang (1..365, default: {DEFAULT_USAGE_DAYS}).",
    )
    p.add_argument(
        "--tz",
        default=DEFAULT_USAGE_TZ,
        help=f"Timezone IANA (default: {DEFAULT_USAGE_TZ}).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Tampilkan hasil dalam format JSON.",
    )
    p.add_argument(
        "--simulate-login",
        metavar="NIS",
        help="Uji login nyata ke Backend + Supabase Auth tanpa Frontend untuk NIS tertentu (mis. --simulate-login 12345678).",
    )
    p.add_argument(
        "--keep-active",
        action="store_true",
        help="Saat memakai --simulate-login, jangan panggil logout agar sesi tetap berstatus 'active'.",
    )
    return p.parse_args()


async def _run_simulated_login(nis: str, *, keep_active: bool) -> int:
    import httpx

    from app.errors import ModelingUnavailable
    from app.main import create_app

    password = os.environ.get("STUDENT_PASSWORD") or getpass.getpass(f"Masukkan password untuk NIS {nis}: ")
    if not password:
        print("Error: Password tidak boleh kosong.", file=sys.stderr)
        return 2

    def _skip_modeling():
        raise ModelingUnavailable("skipped for auth simulation")

    app = create_app(modeling_factory=_skip_modeling)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            print(f"\n[Simulasi] 1. POST /api/student/auth/login (NIS: {nis}) ...")
            r_login = await client.post(
                "/api/student/auth/login",
                json={"nis": nis, "password": password},
            )
            if r_login.status_code != 200:
                print(f"Login gagal (HTTP {r_login.status_code}): {r_login.text}", file=sys.stderr)
                return 1
            data = r_login.json()
            token = data["access_token"]
            visitor_cookie = r_login.cookies.get("lunar_visitor_id")
            print(
                f"[Simulasi]    Login SUKSES sebagai '{data['student']['full_name']}' | Visitor ID (cookie): {visitor_cookie}"
            )

            headers = {"Authorization": f"Bearer {token}"}
            await asyncio.sleep(1)
            print("[Simulasi] 2. POST /api/student/session/heartbeat ...")
            r_hb = await client.post("/api/student/session/heartbeat", headers=headers)
            print(f"[Simulasi]    Heartbeat HTTP {r_hb.status_code}")

            if not keep_active:
                await asyncio.sleep(1)
                print("[Simulasi] 3. POST /api/student/auth/logout ...")
                r_lo = await client.post("/api/student/auth/logout", headers=headers)
                print(f"[Simulasi]    Logout HTTP {r_lo.status_code}\n")
            else:
                print("[Simulasi] 3. Logout dilewati (--keep-active), sesi dibiarkan 'active'.\n")
    return 0


async def main() -> int:
    _load_env_file()
    args = parse_args()
    if not (1 <= args.days <= 365):
        print("Error: --days harus antara 1 sampai 365.", file=sys.stderr)
        return 2

    tz_name = args.tz.strip()
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"Error: Timezone '{tz_name}' tidak valid.", file=sys.stderr)
        return 2

    raw_nis: list[str] | str | None = args.nis if args.nis else None
    if raw_nis is None:
        env_nis = os.environ.get("TRACKED_STUDENT_NIS", "").strip()
        if env_nis:
            raw_nis = env_nis
    try:
        nis_list = parse_nis_values(raw_nis) if raw_nis is not None else None
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL belum di-set (muat backend/.env dulu).", file=sys.stderr)
        return 2

    if args.simulate_login:
        rc = await _run_simulated_login(args.simulate_login.strip(), keep_active=args.keep_active)
        if rc != 0:
            return rc

    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True, prepare_threshold=None)
    try:
        repo = PostgresAdminRepository(conn)
        report = await repo.get_account_usage(nis_list=nis_list, since=since, tz_name=tz_name)
        async with conn.cursor() as cur:
            await cur.execute(
                "select account_type, nis, full_name, ever_used, total_logins, unique_visitors,"
                " last_login_at, active_sessions, total_duration_seconds"
                " from user_login_account_summary order by account_type, nis"
            )
            session_summary = await cur.fetchall()
            await cur.execute(
                "select visitor_id::text, account_type, nis, total_logins, first_login_at, last_login_at,"
                " active_sessions, total_duration_seconds"
                " from user_login_visitor_summary order by last_login_at desc limit 20"
            )
            visitor_summary = await cur.fetchall()
            await cur.execute(
                "select session_id::text, visitor_id::text, account_type, nis, login_at, last_activity_at,"
                " effective_ended_at, session_duration_seconds, session_status"
                " from user_login_sessions_status order by login_at desc limit 20"
            )
            recent_sessions = await cur.fetchall()
    finally:
        await conn.close()

    if args.as_json:
        payload = {
            "timezone": tz_name,
            "days": args.days,
            "accounts": [
                {
                    "student_id": str(a.student_id),
                    "nis": a.nis,
                    "full_name": a.full_name,
                    "active": a.active,
                    "last_sign_in_at": (
                        a.last_sign_in_at.astimezone(zone).isoformat()
                        if a.last_sign_in_at is not None
                        else None
                    ),
                    "login_count": a.login_count,
                    "token_refreshed_count": a.token_refreshed_count,
                    "total_events": a.total_events,
                }
                for a in report.accounts
            ],
            "peak_hours": [vars(b) for b in report.peak_hours],
            "daily_activity": [
                {
                    "date": d.date.isoformat(),
                    "login_count": d.login_count,
                    "token_refreshed_count": d.token_refreshed_count,
                    "total_events": d.total_events,
                }
                for d in report.daily_activity
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"=== TRACKING PENGGUNAAN AKUN ({ args.days } hari terakhir | TZ: { tz_name }) ===\n")
    if not report.accounts:
        print("Tidak ada akun siswa yang cocok.")
        return 0

    print("1. Ringkasan per Akun")
    print(f"{'NIS':<15} {'Nama':<25} {'Login':>8} {'Refresh':>9} {'Total':>8}  {'Terakhir Login'}")
    print("-" * 92)
    for a in report.accounts:
        last_str = (
            a.last_sign_in_at.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")
            if a.last_sign_in_at is not None
            else "Belum pernah"
        )
        print(
            f"{a.nis:<15} {a.full_name[:24]:<25} {a.login_count:>8} {a.token_refreshed_count:>9} {a.total_events:>8}  {last_str}"
        )

    print("\n2. Jam Sibuk Kunjungan (Peak Hours)")
    active_hours = [b for b in report.peak_hours if b.total_events > 0]
    if not active_hours:
        print("Belum ada aktivitas login/refresh pada periode ini.")
    else:
        max_total = max(b.total_events for b in active_hours)
        for b in report.peak_hours:
            bar = "#" * int((b.total_events / max_total) * 20) if max_total > 0 and b.total_events > 0 else ""
            print(
                f"  {b.hour:02d}:00 - {b.hour:02d}:59 | Login: {b.login_count:>4} | Refresh: {b.token_refreshed_count:>4} | Total: {b.total_events:>4}  {bar}"
            )

    print("\n3. Aktivitas Harian")
    if not report.daily_activity:
        print("Belum ada aktivitas harian pada periode ini.")
    else:
        for d in report.daily_activity:
            print(
                f"  {d.date.isoformat()} | Login: {d.login_count:>4} | Refresh: {d.token_refreshed_count:>4} | Total: {d.total_events:>4}"
            )

    print("\n4. Ringkasan Sesi Demo & Judge (Tabel user_login_sessions — ADR-055)")
    if not session_summary:
        print("Belum ada akun bertipe demo/judge atau belum ada sesi di user_login_sessions.")
    else:
        print(
            f"{'Tipe':<8} {'NIS':<15} {'Nama':<22} {'Pernah?':<8} {'Logins':>6} {'Visitors':>8} {'Aktif':>6} {'Durasi(s)':>10}  {'Terakhir Login'}"
        )
        print("-" * 112)
        for acc_type, nis, name, used, logins, visitors, last_in, active_s, dur_s in session_summary:
            last_str = (
                last_in.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")
                if last_in is not None
                else "Belum pernah"
            )
            print(
                f"{(acc_type or '-'):<8} {nis:<15} {name[:21]:<22} {('Ya' if used else 'Tidak'):<8} {logins:>6} {visitors:>8} {active_s:>6} {dur_s:>10}  {last_str}"
            )

    print("\n5. Ringkasan per Anonymous Visitor ID (user_login_visitor_summary)")
    if not visitor_summary:
        print("Belum ada visitor tercatat di user_login_sessions.")
    else:
        for vid, acc_type, nis, logins, first_in, last_in, active_s, dur_s in visitor_summary:
            last_str = last_in.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")
            print(
                f"  Visitor {vid} | {acc_type:<5} (NIS {nis}) | Logins: {logins:>3} | Aktif: {active_s:>2} | Durasi: {dur_s:>5}s | Terakhir: {last_str}"
            )

    print("\n6. Riwayat Sesi Terbaru (user_login_sessions_status)")
    if not recent_sessions:
        print("Belum ada riwayat sesi di user_login_sessions.")
    else:
        for sid, vid, acc_type, nis, login_at, last_act, ended_at, dur_s, status in recent_sessions:
            in_str = login_at.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
            end_str = ended_at.astimezone(zone).strftime("%H:%M:%S") if ended_at else "masih berjalan"
            print(
                f"  [{status:<10}] {acc_type:<5} (NIS {nis}) | Login: {in_str} | Selesai: {end_str:<14} | Durasi: {dur_s:>4}s | Visitor: {(vid or '-')[:8]}… | Session: {sid[:8]}…"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
