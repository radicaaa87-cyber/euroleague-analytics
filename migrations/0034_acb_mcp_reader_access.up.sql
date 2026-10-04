-- Allow the hosted read-only MCP role to serve the three source-native
-- ACB surfaces added to the registry. No write privilege is granted.
grant select on table
  public.acb_game,
  public.acb_player_game,
  public.acb_event
to el_reader;
