-- modules/tournaments/20251014_tournaments.sql
create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

-- أنواع البطولات
create table if not exists tournament_types(
  key text primary key,                -- solo|duo|squad
  label text not null,
  team_size int not null,              -- 1|2|4
  max_teams int not null,              -- 100|50|25
  enabled boolean not null default true
);

insert into tournament_types(key,label,team_size,max_teams) values
 ('solo','بطولة سولو 1vs100',1,100),
 ('duo','بطولة دو 2vs100',2,50),
 ('squad','بطولة سكواد 4vs100',4,25)
on conflict(key) do nothing;

-- بطولة جارية لكل نوع
create table if not exists tournaments(
  id uuid primary key default gen_random_uuid(),
  type_key text not null references tournament_types(key),
  status text not null default 'open' check (status in ('open','closed')),
  entry_fee int not null default 2000,
  prize_min int not null default 325,
  created_at timestamptz not null default now()
);

-- المشاركات
create table if not exists tournament_entries(
  id uuid primary key default gen_random_uuid(),
  tournament_id uuid not null references tournaments(id) on delete cascade,
  user_id bigint not null,
  team_number int not null,
  pubg_id text,
  phone text,
  payment_captured boolean not null default false,
  created_at timestamptz not null default now(),
  unique(tournament_id, user_id)
);
create index if not exists idx_t_entries_tn on tournament_entries(tournament_id,team_number);
create index if not exists idx_t_entries_user on tournament_entries(user_id);

-- أكواد انضمام الفرق لضبط من ينضم لنفس الرقم
create table if not exists tournament_team_codes(
  tournament_id uuid not null references tournaments(id) on delete cascade,
  team_number int not null,
  join_code text not null,
  created_at timestamptz not null default now(),
  primary key(tournament_id, team_number)
);

-- حجم الفريق للبطولة
create or replace function team_size_for_tournament(p_tournament uuid)
returns int language sql stable as $$
  select tt.team_size from tournaments t join tournament_types tt on t.type_key=tt.key
  where t.id=p_tournament
$$;

-- الأرقام المتاحة بوظيفة تُعيد السعات المتبقية
create or replace function available_team_numbers(p_tournament uuid)
returns table(team_number int, slots_left int) language sql stable as $$
  with ts as (select team_size_for_tournament(p_tournament) as sz),
  counts as (
    select team_number, count(*) c
    from tournament_entries
    where tournament_id=p_tournament
    group by team_number
  ),
  lim as (
    select case tt.key when 'solo' then 100 when 'duo' then 50 when 'squad' then 25 end as maxn
    from tournaments t join tournament_types tt on t.type_key=tt.key where t.id=p_tournament
  )
  select n as team_number, (select sz from ts) - coalesce(c.c,0) as slots_left
  from generate_series(1,(select maxn from lim)) g(n)
  left join counts c on c.team_number=n
  where (select sz from ts) - coalesce(c.c,0) > 0
  order by n;
$$;

-- حجز مقعد بفريق رقم معيّن مع قفل استشاري + رمز انضمام
create or replace function reserve_team_slot(
  p_tournament uuid, p_user bigint, p_num int, p_join_code text default null
) returns uuid
language plpgsql as $$
declare v_size int; v_count int; v_id uuid; v_code text;
begin
  v_size := team_size_for_tournament(p_tournament);
  if v_size is null then raise exception 'invalid_tournament'; end if;

  perform pg_advisory_xact_lock(hashtextextended(p_tournament::text,0)::bigint, p_num);

  select count(*) into v_count from tournament_entries
   where tournament_id=p_tournament and team_number=p_num;

  if v_count=0 then
    v_code := coalesce(p_join_code, encode(digest(gen_random_uuid()::text,'sha256'),'hex'));
    insert into tournament_team_codes(tournament_id,team_number,join_code)
    values(p_tournament,p_num,v_code) on conflict do nothing;
  else
    select join_code into v_code from tournament_team_codes
    where tournament_id=p_tournament and team_number=p_num;
    if v_code is null then raise exception 'team_state_error'; end if;
    if p_join_code is null or p_join_code<>v_code then
      raise exception 'join_code_required' using hint='رمز الفريق غير صحيح';
    end if;
  end if;

  if v_count >= v_size then raise exception 'team_full' using hint='الفريق ممتلئ'; end if;

  insert into tournament_entries(tournament_id,user_id,team_number)
  values(p_tournament,p_user,p_num)
  returning id into v_id;
  return v_id;
end; $$;

create or replace function get_team_join_code(p_tournament uuid, p_num int)
returns text language sql stable as $$
  select join_code from tournament_team_codes
  where tournament_id=p_tournament and team_number=p_num
$$;
