do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'storage') then
    if to_regclass('storage.objects') is not null
       and exists (
         select 1
         from storage.objects
         where bucket_id = 'model-artifacts'
       ) then
      raise exception
        'Refusing to drop non-empty model-artifacts bucket';
    end if;

    delete from storage.buckets
    where id = 'model-artifacts';
  end if;
end
$$;
