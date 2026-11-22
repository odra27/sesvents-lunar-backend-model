-- ADR-057 — Extend Login Session Tracking (`user_login_sessions`) to ALL Student Accounts.
--
-- Non-destructive & idempotent:
--   1. Relaxes CHECK constraints on `student_profiles.account_type` and
--      `user_login_sessions.account_type` to allow `'student'` alongside `'demo'` and `'judge'`.
--   2. Updates `user_login_account_summary` view so every student account (Demo, Judge, and
--      self-registered / regular students) is included with `account_type` defaulting to `'student'`.

alter table student_profiles
  drop constraint if exists student_profiles_account_type_check;

alter table student_profiles
  add constraint student_profiles_account_type_check
  check (account_type in ('demo', 'judge', 'student'));

alter table user_login_sessions
  drop constraint if exists user_login_sessions_account_type_check;

alter table user_login_sessions
  add constraint user_login_sessions_account_type_check
  check (account_type in ('demo', 'judge', 'student'));

create or replace view user_login_account_summary as
select
  sp.id as user_id,
  u.email,
  sp.nis,
  sp.full_name,
  coalesce(sp.account_type, max(uls.account_type), 'student') as account_type,
  count(uls.session_id) > 0 as ever_used,
  count(uls.session_id)::integer as total_logins,
  count(distinct uls.visitor_id)::integer as unique_visitors,
  (max(uls.login_at) at time zone 'Asia/Jakarta') as last_login_at_wib,
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
left join auth.users u on u.id = sp.id
left join user_login_sessions uls on uls.user_id = sp.id
group by sp.id, u.email, sp.nis, sp.full_name, sp.account_type;
