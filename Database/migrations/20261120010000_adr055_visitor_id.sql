-- ADR-055 (Part 2) — Anonymous Visitor Identification (`visitor_id`) & WIB reporting columns.
--
-- Non-destructive & idempotent:
--   1. Adds `visitor_id uuid` to `user_login_sessions` so multiple anonymous visitors sharing
--      the same Demo or Judge credentials can be distinguished without browser fingerprinting
--      or storing IP addresses.
--   2. Adds indexes on `(account_type, visitor_id)` and `(visitor_id, login_at desc)`.
--   3. Recreates read-only reporting views (`user_login_sessions_status`,
--      `user_login_account_summary`, and `user_login_visitor_summary`) including explicit
--      `*_wib` (`AT TIME ZONE 'Asia/Jakarta'`) timestamp columns so Supabase Dashboard displays
--      local WIB (UTC+7) wall-clock times directly.

alter table user_login_sessions
  add column if not exists visitor_id uuid,
  add column if not exists login_at_wib timestamp generated always as (login_at at time zone 'Asia/Jakarta') stored,
  add column if not exists last_activity_at_wib timestamp generated always as (last_activity_at at time zone 'Asia/Jakarta') stored,
  add column if not exists logout_at_wib timestamp generated always as (logout_at at time zone 'Asia/Jakarta') stored;

create index if not exists user_login_sessions_account_visitor_idx
  on user_login_sessions (account_type, visitor_id)
  where visitor_id is not null;

create index if not exists user_login_sessions_visitor_login_idx
  on user_login_sessions (visitor_id, login_at desc)
  where visitor_id is not null;

drop view if exists user_login_visitor_summary;
drop view if exists user_login_account_summary;
drop view if exists user_login_sessions_status;

create view user_login_sessions_status as
select
  uls.session_id,
  uls.user_id,
  uls.visitor_id,
  sp.nis,
  sp.full_name,
  uls.account_type,
  (uls.login_at at time zone 'Asia/Jakarta') as login_at_wib,
  (uls.last_activity_at at time zone 'Asia/Jakarta') as last_activity_at_wib,
  (uls.logout_at at time zone 'Asia/Jakarta') as logout_at_wib,
  (
    case
      when uls.logout_at is not null then uls.logout_at
      when now() - uls.last_activity_at > interval '30 minutes' then uls.last_activity_at
      else null
    end at time zone 'Asia/Jakarta'
  ) as effective_ended_at_wib,
  greatest(
    0,
    extract(epoch from (coalesce(uls.logout_at, uls.last_activity_at) - uls.login_at))::integer
  ) as session_duration_seconds,
  case
    when uls.logout_at is not null then 'logged_out'
    when now() - uls.last_activity_at <= interval '30 minutes' then 'active'
    else 'expired'
  end as session_status,
  uls.login_at,
  uls.last_activity_at,
  uls.logout_at,
  case
    when uls.logout_at is not null then uls.logout_at
    when now() - uls.last_activity_at > interval '30 minutes' then uls.last_activity_at
    else null
  end as effective_ended_at
from user_login_sessions uls
join student_profiles sp on sp.id = uls.user_id;

create view user_login_account_summary as
select
  sp.id as user_id,
  sp.nis,
  sp.full_name,
  coalesce(sp.account_type, max(uls.account_type)) as account_type,
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
left join user_login_sessions uls on uls.user_id = sp.id
where sp.account_type in ('demo', 'judge') or uls.session_id is not null
group by sp.id, sp.nis, sp.full_name, sp.account_type;

create view user_login_visitor_summary as
select
  uls.visitor_id,
  uls.account_type,
  uls.user_id,
  sp.nis,
  count(uls.session_id)::integer as total_logins,
  (min(uls.login_at) at time zone 'Asia/Jakarta') as first_login_at_wib,
  (max(uls.login_at) at time zone 'Asia/Jakarta') as last_login_at_wib,
  min(uls.login_at) as first_login_at,
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
from user_login_sessions uls
join student_profiles sp on sp.id = uls.user_id
where uls.visitor_id is not null
group by uls.visitor_id, uls.account_type, uls.user_id, sp.nis;
