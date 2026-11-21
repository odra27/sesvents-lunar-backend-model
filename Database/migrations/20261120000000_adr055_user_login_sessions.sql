-- ADR-055 — Login Activity Tracking for Demo and Judge student accounts.
--
-- Non-destructive & idempotent:
--   1. Adds nullable `student_profiles.account_type` ('demo' | 'judge') for backend/DB account tagging.
--   2. Creates `user_login_sessions` to record every login session without overwriting history.
--   3. Enables RLS without policies (same defense-in-depth model as ADR-048 / ADR-051 / ADR-053).
--   4. Creates read-only SQL views (`user_login_sessions_status`, `user_login_account_summary`)
--      that dynamically mark sessions as 'active', 'logged_out', or 'expired' (30-minute inactivity
--      threshold) so sessions without an explicit logout are never treated as active forever.

alter table student_profiles
  add column if not exists account_type text
  check (account_type in ('demo', 'judge'));

create table if not exists user_login_sessions (
  session_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references student_profiles(id) on delete cascade,
  account_type text not null check (account_type in ('demo', 'judge')),
  auth_session_id text,
  login_at timestamptz not null default now(),
  last_activity_at timestamptz not null default now(),
  logout_at timestamptz,
  session_duration_seconds integer not null default 0 check (session_duration_seconds >= 0),
  check (last_activity_at >= login_at),
  check (logout_at is null or logout_at >= login_at)
);

create index if not exists user_login_sessions_user_login_idx
  on user_login_sessions (user_id, login_at desc);

create index if not exists user_login_sessions_account_type_login_idx
  on user_login_sessions (account_type, login_at desc);

create index if not exists user_login_sessions_active_idx
  on user_login_sessions (user_id, last_activity_at desc)
  where logout_at is null;

create index if not exists user_login_sessions_auth_session_idx
  on user_login_sessions (user_id, auth_session_id)
  where auth_session_id is not null and logout_at is null;

alter table user_login_sessions enable row level security;

-- Read-only view: dynamic session status with 30-minute inactivity timeout.
create or replace view user_login_sessions_status as
select
  uls.session_id,
  uls.user_id,
  sp.nis,
  sp.full_name,
  uls.account_type,
  uls.login_at,
  uls.last_activity_at,
  uls.logout_at,
  case
    when uls.logout_at is not null then uls.logout_at
    when now() - uls.last_activity_at > interval '30 minutes' then uls.last_activity_at
    else null
  end as effective_ended_at,
  greatest(
    0,
    extract(epoch from (coalesce(uls.logout_at, uls.last_activity_at) - uls.login_at))::integer
  ) as session_duration_seconds,
  case
    when uls.logout_at is not null then 'logged_out'
    when now() - uls.last_activity_at <= interval '30 minutes' then 'active'
    else 'expired'
  end as session_status
from user_login_sessions uls
join student_profiles sp on sp.id = uls.user_id;

-- Read-only view: per-account summary for Demo and Judge accounts.
create or replace view user_login_account_summary as
select
  sp.id as user_id,
  sp.nis,
  sp.full_name,
  coalesce(sp.account_type, max(uls.account_type)) as account_type,
  count(uls.session_id) > 0 as ever_used,
  count(uls.session_id)::integer as total_logins,
  max(uls.login_at) as last_login_at,
  count(uls.session_id) filter (
    where uls.logout_at is null
      and now() - uls.last_activity_at <= interval '30 minutes'
  )::integer as active_sessions,
  coalesce(
    sum(
      greatest(
        0,
        extract(epoch from (coalesce(uls.logout_at, uls.last_activity_at) - uls.login_at))::integer
      )
    ),
    0
  )::integer as total_duration_seconds
from student_profiles sp
left join user_login_sessions uls on uls.user_id = sp.id
where sp.account_type in ('demo', 'judge') or uls.session_id is not null
group by sp.id, sp.nis, sp.full_name, sp.account_type;
