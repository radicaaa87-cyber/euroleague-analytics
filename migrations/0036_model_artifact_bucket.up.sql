do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'storage') then
    insert into storage.buckets (id, name, public)
    values ('model-artifacts', 'model-artifacts', false)
    on conflict (id) do update
      set name = excluded.name,
          public = false;
  end if;
end
$$;
