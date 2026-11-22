-- ADR-056 — B2C Email Login, Self-Registration, and Optional School Class Code.
--
-- Non-destructive & idempotent:
--   1. Allows `student_profiles.nis` and `student_profiles.institution_id` to be NULL so B2C
--      students can register and log in with email + password without an NIS or school code.
--   2. Adds optional `student_profiles.class_code` (e.g. 'LNR-5A21') for optional B2B school link.
--   3. Updates reporting views (`user_login_sessions_status`, `user_login_account_summary`,
--      `user_login_visitor_summary`) to include `email` from `auth.users`.

alter table student_profiles
  alter column nis drop not null,
  alter column institution_id drop not null,
  add column if not exists class_code varchar(30);

drop view if exists user_login_visitor_summary;
drop view if exists user_login_account_summary;
drop view if exists user_login_sessions_status;

create view user_login_sessions_status as
select
  uls.session_id,
  uls.user_id,
  uls.visitor_id,
  u.email,
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
join student_profiles sp on sp.id = uls.user_id
left join auth.users u on u.id = sp.id;

create view user_login_account_summary as
select
  sp.id as user_id,
  u.email,
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
left join auth.users u on u.id = sp.id
left join user_login_sessions uls on uls.user_id = sp.id
where sp.account_type in ('demo', 'judge') or uls.session_id is not null
group by sp.id, u.email, sp.nis, sp.full_name, sp.account_type;

create view user_login_visitor_summary as
select
  uls.visitor_id,
  uls.account_type,
  uls.user_id,
  u.email,
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
left join auth.users u on u.id = sp.id
where uls.visitor_id is not null
group by uls.visitor_id, uls.account_type, uls.user_id, u.email, sp.nis;
