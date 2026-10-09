-- =============================================================================
-- 03_xx_coa_map_loader_pkg.sql
--
-- All business rules for the Finance COA mapping load live here, so the same
-- logic runs whether the staging tables were filled by the Unix script
-- (SQL*Loader) or by Oracle Integration Cloud (DB adapter).
--
-- Flow for one batch (one input workbook):
--   1. resolve MAPPING_SET_NAME -> existing MAPPING_SET_ID (case-insensitive)
--   2. validate every staged row; any error => whole batch rejected, nothing
--      touches the business tables, errors are left in XX_COA_MAP_ERRORS_V
--   3. create missing MAPPING_SET rows (sequence MAPPING_SET_S)
--   4. MERGE MAPPING_SET_VALUES on MAPPING_SET_ID + TARGET1
--        - existing row: MAPPING_SET_VALUE_ID / MAPPING_SET_ID kept, targets updated
--        - new row:      MAPPING_SET_VALUE_ID from MAPPING_SET_VALUES_S
--   5. MERGE COA_MAPPINGS on MAPPING_SET_ID + SEGMENT1 + SEGMENT2
--        - existing row: MAP_ID kept, description/targets updated
--        - new row:      MAP_ID from COA_MAPPINGS_S
--   6. single COMMIT (all-or-nothing)
-- =============================================================================

create or replace package xx_coa_map_loader_pkg authid definer as

  -- Opens a batch and returns its id. Called before staging is loaded.
  function create_batch(p_file_name in varchar2) return number;

  -- Validates and applies a staged batch. Never raises: the outcome is in
  -- XX_COA_MAP_BATCH.STATUS (SUCCESS / ERROR) and MESSAGE.
  --   p_validate_targets  Y = every TARGETn on a COA row must exist in
  --                           MAPPING_SET_VALUES (or in the same file)
  --   p_overwrite_blanks  N = a blank cell keeps the value already in the table
  --                       Y = a blank cell clears it
  --   p_create_sets       Y = unknown MAPPING_SET_NAME creates the master row
  --                       N = unknown MAPPING_SET_NAME is an error
  procedure process_batch(p_batch_id         in number,
                          p_validate_targets in varchar2 default 'Y',
                          p_overwrite_blanks in varchar2 default 'N',
                          p_create_sets      in varchar2 default 'Y');

  -- Marks a batch failed for a reason outside the database (bad file, loader error).
  procedure fail_batch(p_batch_id in number, p_message in varchar2);

  function batch_status(p_batch_id in number) return varchar2;

  -- Removes staging and batch history older than p_days.
  procedure purge(p_days in number default 90);

  -- Public only so dynamic SQL can call it; not part of the API.
  function append_msg(p_old in varchar2, p_new in varchar2) return varchar2;

end xx_coa_map_loader_pkg;
/

create or replace package body xx_coa_map_loader_pkg as

  c_max_targets constant pls_integer   := 10;
  c_user_name   constant varchar2(30)  := 'COA_MAP_LOADER';
  c_user_id     constant number        := -1;     -- for NUMBER audit columns
  c_stg_val     constant varchar2(30)  := 'XX_COA_MAP_STG_VALUES';
  c_stg_coa     constant varchar2(30)  := 'XX_COA_MAP_STG_COA';

  -- ---------------------------------------------------------------------------
  -- helpers
  -- ---------------------------------------------------------------------------

  function append_msg(p_old in varchar2, p_new in varchar2) return varchar2 is
  begin
    return substr(case when p_old is null then p_new else p_old || '; ' || p_new end, 1, 4000);
  end append_msg;

  function has_col(p_tab in varchar2, p_col in varchar2) return boolean is
    l_cnt pls_integer;
  begin
    select count(*) into l_cnt
      from user_tab_columns
     where table_name = p_tab and column_name = p_col;
    return l_cnt > 0;
  end has_col;

  function col_len(p_tab in varchar2, p_col in varchar2) return number is
    l_len number;
  begin
    select char_length into l_len
      from user_tab_columns
     where table_name = p_tab and column_name = p_col;
    return l_len;
  exception
    when no_data_found then return null;
  end col_len;

  -- Audit column lists for whichever of the standard columns the table has.
  procedure audit_lists(p_tab      in  varchar2,
                        p_ins_cols out varchar2,
                        p_ins_vals out varchar2,
                        p_upd      out varchar2) is

    procedure add(p_col in varchar2, p_on_update in boolean) is
      l_type user_tab_columns.data_type%type;
      l_expr varchar2(100);
    begin
      select data_type into l_type
        from user_tab_columns
       where table_name = p_tab and column_name = p_col;
      l_expr := case
                  when p_col like '%DATE'  then 'sysdate'
                  when l_type = 'NUMBER'   then to_char(c_user_id)
                  else '''' || c_user_name || ''''
                end;
      p_ins_cols := p_ins_cols || ', ' || p_col;
      p_ins_vals := p_ins_vals || ', ' || l_expr;
      if p_on_update then
        p_upd := p_upd || ', t.' || p_col || ' = ' || l_expr;
      end if;
    exception
      when no_data_found then null;
    end add;

  begin
    add('CREATED_BY',       false);
    add('CREATION_DATE',    false);
    add('LAST_UPDATED_BY',  true);
    add('LAST_UPDATE_DATE', true);
  end audit_lists;

  -- Flags staged rows of p_stg matching p_where as errors.
  -- p_msg is a SQL expression (may reference columns of alias s).
  procedure flag(p_stg in varchar2, p_batch in number, p_where in varchar2, p_msg in varchar2) is
  begin
    execute immediate
         'update ' || p_stg || ' s'
      || '   set s.status = ''E'','
      || '       s.error_msg = xx_coa_map_loader_pkg.append_msg(s.error_msg, ' || p_msg || ')'
      || ' where s.batch_id = :b and (' || p_where || ')'
      using p_batch;
  end flag;

  -- Value too long for (or column missing from) the business table.
  procedure check_len(p_stg in varchar2, p_batch in number, p_col in varchar2, p_tab in varchar2) is
    l_len number := col_len(p_tab, p_col);
  begin
    if l_len is null then
      flag(p_stg, p_batch, 's.' || p_col || ' is not null',
           '''' || p_col || ' has a value but ' || p_tab || ' has no such column''');
    else
      flag(p_stg, p_batch, 'length(s.' || p_col || ') > ' || l_len,
           '''' || p_col || ' is longer than ' || l_len || ' characters''');
    end if;
  end check_len;

  procedure flag_duplicates(p_stg in varchar2, p_batch in number, p_key in varchar2, p_label in varchar2) is
  begin
    flag(p_stg, p_batch,
         's.rowid in (select rid from (select rowid rid, count(*) over (partition by ' || p_key || ') cnt'
      || '  from ' || p_stg || ' where batch_id = ' || to_char(p_batch) || ') where cnt > 1)',
         '''Duplicate ' || p_label || ' - appears more than once in the file''');
  end flag_duplicates;

  -- Trims every staged value (OIC does not run the Python converter) and
  -- resets row state so a batch can be re-processed after staging is fixed.
  -- Returns the number of rows staged in p_stg.
  function normalise(p_stg in varchar2, p_batch in number) return number is
    l_set varchar2(4000) := 'mapping_set_name = trim(mapping_set_name)';
  begin
    if p_stg = c_stg_coa then
      l_set := l_set || ', segment1 = trim(segment1), segment2 = trim(segment2)'
                     || ', description = trim(description)';
    end if;
    for k in 1 .. c_max_targets loop
      l_set := l_set || ', target' || k || ' = trim(target' || k || ')';
    end loop;
    execute immediate
         'update ' || p_stg
      || '   set ' || l_set || ', status = ''N'', error_msg = null, mapping_set_id = null'
      || ' where batch_id = :b'
      using p_batch;
    return sql%rowcount;
  end normalise;

  -- ---------------------------------------------------------------------------
  -- mapping set resolution
  -- ---------------------------------------------------------------------------

  procedure resolve_sets(p_batch in number) is
  begin
    update xx_coa_map_stg_values s
       set s.mapping_set_id = (select min(m.mapping_set_id)
                                 from mapping_set m
                                where upper(trim(m.mapping_set_name)) = upper(s.mapping_set_name))
     where s.batch_id = p_batch
       and s.mapping_set_id is null;

    update xx_coa_map_stg_coa s
       set s.mapping_set_id = (select min(m.mapping_set_id)
                                 from mapping_set m
                                where upper(trim(m.mapping_set_name)) = upper(s.mapping_set_name))
     where s.batch_id = p_batch
       and s.mapping_set_id is null;
  end resolve_sets;

  function create_sets(p_batch in number) return number is
    l_ic varchar2(400); l_iv varchar2(400); l_up varchar2(400);
  begin
    audit_lists('MAPPING_SET', l_ic, l_iv, l_up);
    execute immediate
         'insert into mapping_set (mapping_set_id, mapping_set_name' || l_ic || ')'
      || ' select mapping_set_s.nextval, n.name' || l_iv
      || '   from (select min(mapping_set_name) name'
      || '           from (select mapping_set_name from ' || c_stg_val
      || '                  where batch_id = :b1 and mapping_set_id is null'
      || '                 union all'
      || '                 select mapping_set_name from ' || c_stg_coa
      || '                  where batch_id = :b2 and mapping_set_id is null)'
      || '          group by upper(mapping_set_name)) n'
      using p_batch, p_batch;
    return sql%rowcount;
  end create_sets;

  -- ---------------------------------------------------------------------------
  -- validation
  -- ---------------------------------------------------------------------------

  procedure validate(p_batch            in number,
                     p_validate_targets in varchar2,
                     p_create_sets      in varchar2) is
    l_col        varchar2(30);
    l_coa_by_set boolean := has_col('COA_MAPPINGS', 'MAPPING_SET_ID');
  begin
    -- ---- MAPPING_SET_VALUES sheet ----
    flag(c_stg_val, p_batch, 's.mapping_set_name is null', '''MAPPING_SET_NAME is required''');
    flag(c_stg_val, p_batch, 's.target1 is null', '''TARGET1 is required (it identifies the value row)''');
    check_len(c_stg_val, p_batch, 'MAPPING_SET_NAME', 'MAPPING_SET');
    for k in 1 .. c_max_targets loop
      check_len(c_stg_val, p_batch, 'TARGET' || k, 'MAPPING_SET_VALUES');
    end loop;
    flag_duplicates(c_stg_val, p_batch, 'upper(mapping_set_name), target1', 'MAPPING_SET_NAME + TARGET1');

    -- ---- COA_MAPPINGS sheet ----
    flag(c_stg_coa, p_batch, 's.mapping_set_name is null', '''MAPPING_SET_NAME is required''');
    flag(c_stg_coa, p_batch, 's.segment1 is null', '''SEGMENT1 is required''');
    flag(c_stg_coa, p_batch, 's.segment2 is null', '''SEGMENT2 is required''');
    check_len(c_stg_coa, p_batch, 'MAPPING_SET_NAME', 'MAPPING_SET');
    check_len(c_stg_coa, p_batch, 'SEGMENT1', 'COA_MAPPINGS');
    check_len(c_stg_coa, p_batch, 'SEGMENT2', 'COA_MAPPINGS');
    if has_col('COA_MAPPINGS', 'DESCRIPTION') then   -- otherwise the column is simply ignored
      check_len(c_stg_coa, p_batch, 'DESCRIPTION', 'COA_MAPPINGS');
    end if;
    for k in 1 .. c_max_targets loop
      check_len(c_stg_coa, p_batch, 'TARGET' || k, 'COA_MAPPINGS');
    end loop;
    if l_coa_by_set then
      flag_duplicates(c_stg_coa, p_batch, 'upper(mapping_set_name), segment1, segment2',
                      'MAPPING_SET_NAME + SEGMENT1 + SEGMENT2');
    else
      flag_duplicates(c_stg_coa, p_batch, 'segment1, segment2', 'SEGMENT1 + SEGMENT2');
    end if;

    -- ---- mapping set master ----
    for t in (select c_stg_val tab from dual union all select c_stg_coa from dual) loop
      flag(t.tab, p_batch,
           '(select count(*) from mapping_set m'
        || '  where upper(trim(m.mapping_set_name)) = upper(s.mapping_set_name)) > 1',
           '''MAPPING_SET_NAME "'' || s.mapping_set_name || ''" matches more than one MAPPING_SET row''');
      if p_create_sets = 'N' then
        flag(t.tab, p_batch, 's.mapping_set_name is not null and s.mapping_set_id is null',
             '''MAPPING_SET_NAME "'' || s.mapping_set_name || ''" does not exist in MAPPING_SET''');
      end if;
    end loop;

    -- ---- COA targets must be defined values of the mapping set ----
    if p_validate_targets = 'Y' then
      for k in 1 .. c_max_targets loop
        l_col := 'TARGET' || k;
        if has_col('MAPPING_SET_VALUES', l_col) then
          flag(c_stg_coa, p_batch,
               's.' || l_col || ' is not null'
            || ' and not exists (select 1 from mapping_set_values v'
            || '                  where v.mapping_set_id = s.mapping_set_id'
            || '                    and v.' || l_col || ' = s.' || l_col || ')'
            || ' and not exists (select 1 from ' || c_stg_val || ' sv'
            || '                  where sv.batch_id = s.batch_id'
            || '                    and upper(sv.mapping_set_name) = upper(s.mapping_set_name)'
            || '                    and sv.' || l_col || ' = s.' || l_col || ')',
               '''' || l_col || ' "'' || s.' || l_col
            || ' || ''" is not defined in MAPPING_SET_VALUES for this mapping set''');
        end if;
      end loop;
    end if;
  end validate;

  -- ---------------------------------------------------------------------------
  -- merges
  -- ---------------------------------------------------------------------------

  function upd_expr(p_col in varchar2, p_overwrite in varchar2) return varchar2 is
  begin
    return 't.' || p_col || ' = '
        || case when p_overwrite = 'Y' then 's.' || p_col
                else 'nvl(s.' || p_col || ', t.' || p_col || ')' end;
  end upd_expr;

  procedure merge_values(p_batch in number, p_overwrite in varchar2,
                         p_ins out number, p_upd out number) is
    l_cols varchar2(4000);
    l_vals varchar2(4000);
    l_set  varchar2(4000);
    l_ic varchar2(400); l_iv varchar2(400); l_up varchar2(400);
    l_col  varchar2(30);
    l_sql  varchar2(32767);
  begin
    for k in 1 .. c_max_targets loop
      l_col := 'TARGET' || k;
      if has_col('MAPPING_SET_VALUES', l_col) then
        l_cols := l_cols || ', ' || l_col;
        l_vals := l_vals || ', s.' || l_col;
        if k > 1 then
          l_set := l_set || ', ' || upd_expr(l_col, p_overwrite);
        end if;
      end if;
    end loop;
    audit_lists('MAPPING_SET_VALUES', l_ic, l_iv, l_up);
    l_set := ltrim(l_set || l_up, ', ');

    select count(*) into p_upd
      from xx_coa_map_stg_values s
     where s.batch_id = p_batch
       and s.status = 'V'
       and exists (select 1 from mapping_set_values t
                    where t.mapping_set_id = s.mapping_set_id
                      and t.target1 = s.target1);

    l_sql := 'merge into mapping_set_values t'
          || ' using (select s.mapping_set_id' || l_vals
          || '          from ' || c_stg_val || ' s'
          || '         where s.batch_id = :b and s.status = ''V'') s'
          || ' on (t.mapping_set_id = s.mapping_set_id and t.target1 = s.target1)';
    if l_set is not null then
      l_sql := l_sql || ' when matched then update set ' || l_set;
    end if;
    l_sql := l_sql
          || ' when not matched then insert (mapping_set_value_id, mapping_set_id' || l_cols || l_ic || ')'
          || ' values (mapping_set_values_s.nextval, s.mapping_set_id' || l_vals || l_iv || ')';

    execute immediate l_sql using p_batch;
    p_ins := sql%rowcount - case when l_set is not null then p_upd else 0 end;
  end merge_values;

  procedure merge_coa(p_batch in number, p_overwrite in varchar2,
                      p_ins out number, p_upd out number) is
    l_by_set boolean := has_col('COA_MAPPINGS', 'MAPPING_SET_ID');
    l_cols varchar2(4000) := 'segment1, segment2';
    l_vals varchar2(4000) := 's.segment1, s.segment2';
    l_set  varchar2(4000);
    l_on   varchar2(400)  := 't.segment1 = s.segment1 and t.segment2 = s.segment2';
    l_ic varchar2(400); l_iv varchar2(400); l_up varchar2(400);
    l_col  varchar2(30);
    l_sql  varchar2(32767);
  begin
    if l_by_set then
      l_cols := 'mapping_set_id, ' || l_cols;
      l_vals := 's.mapping_set_id, ' || l_vals;
      l_on   := 't.mapping_set_id = s.mapping_set_id and ' || l_on;
    end if;
    if has_col('COA_MAPPINGS', 'DESCRIPTION') then
      l_cols := l_cols || ', description';
      l_vals := l_vals || ', s.description';
      l_set  := l_set  || ', ' || upd_expr('DESCRIPTION', p_overwrite);
    end if;
    for k in 1 .. c_max_targets loop
      l_col := 'TARGET' || k;
      if has_col('COA_MAPPINGS', l_col) then
        l_cols := l_cols || ', ' || l_col;
        l_vals := l_vals || ', s.' || l_col;
        l_set  := l_set  || ', ' || upd_expr(l_col, p_overwrite);
      end if;
    end loop;
    audit_lists('COA_MAPPINGS', l_ic, l_iv, l_up);
    l_set := ltrim(l_set || l_up, ', ');

    execute immediate
         'select count(*) from ' || c_stg_coa || ' s'
      || ' where s.batch_id = :b and s.status = ''V'''
      || '   and exists (select 1 from coa_mappings t where ' || l_on || ')'
      into p_upd using p_batch;

    l_sql := 'merge into coa_mappings t'
          || ' using (select s.* from ' || c_stg_coa || ' s'
          || '         where s.batch_id = :b and s.status = ''V'') s'
          || ' on (' || l_on || ')';
    if l_set is not null then
      l_sql := l_sql || ' when matched then update set ' || l_set;
    end if;
    l_sql := l_sql
          || ' when not matched then insert (map_id, ' || l_cols || l_ic || ')'
          || ' values (coa_mappings_s.nextval, ' || l_vals || l_iv || ')';

    execute immediate l_sql using p_batch;
    p_ins := sql%rowcount - case when l_set is not null then p_upd else 0 end;
  end merge_coa;

  -- ---------------------------------------------------------------------------
  -- public API
  -- ---------------------------------------------------------------------------

  function create_batch(p_file_name in varchar2) return number is
    l_id number;
  begin
    insert into xx_coa_map_batch (batch_id, file_name, status)
    values (xx_coa_map_batch_s.nextval, substr(p_file_name, 1, 400), 'NEW')
    returning batch_id into l_id;
    commit;
    return l_id;
  end create_batch;

  procedure fail_batch(p_batch_id in number, p_message in varchar2) is
  begin
    update xx_coa_map_batch
       set status = 'ERROR',
           message = substr(p_message, 1, 4000),
           finished_on = systimestamp
     where batch_id = p_batch_id;
    commit;
  end fail_batch;

  function batch_status(p_batch_id in number) return varchar2 is
    l_status xx_coa_map_batch.status%type;
  begin
    select status into l_status from xx_coa_map_batch where batch_id = p_batch_id;
    return l_status;
  exception
    when no_data_found then return 'UNKNOWN';
  end batch_status;

  procedure process_batch(p_batch_id         in number,
                          p_validate_targets in varchar2 default 'Y',
                          p_overwrite_blanks in varchar2 default 'N',
                          p_create_sets      in varchar2 default 'Y') is
    l_val_rows  number;
    l_coa_rows  number;
    l_errors    number;
    l_sets      number := 0;
    l_val_ins   number := 0;
    l_val_upd   number := 0;
    l_coa_ins   number := 0;
    l_coa_upd   number := 0;
  begin
    l_val_rows := normalise(c_stg_val, p_batch_id);
    l_coa_rows := normalise(c_stg_coa, p_batch_id);

    update xx_coa_map_batch
       set status = 'RUNNING', started_on = systimestamp, finished_on = null,
           values_rows = l_val_rows, coa_rows = l_coa_rows, message = null
     where batch_id = p_batch_id;
    commit;

    if l_val_rows + l_coa_rows = 0 then
      fail_batch(p_batch_id, 'No data rows were staged for this batch');
      return;
    end if;

    -- serialise loads so two batches cannot create the same mapping set
    lock table mapping_set in share row exclusive mode;

    resolve_sets(p_batch_id);
    validate(p_batch_id, p_validate_targets, p_create_sets);

    select (select count(*) from xx_coa_map_stg_values where batch_id = p_batch_id and status = 'E')
         + (select count(*) from xx_coa_map_stg_coa    where batch_id = p_batch_id and status = 'E')
      into l_errors
      from dual;

    if l_errors > 0 then
      update xx_coa_map_batch
         set status = 'ERROR', error_rows = l_errors, finished_on = systimestamp,
             message = l_errors || ' row(s) failed validation - nothing was loaded. See XX_COA_MAP_ERRORS_V.'
       where batch_id = p_batch_id;
      commit;
      return;
    end if;

    update xx_coa_map_stg_values set status = 'V' where batch_id = p_batch_id;
    update xx_coa_map_stg_coa    set status = 'V' where batch_id = p_batch_id;

    if p_create_sets = 'Y' then
      l_sets := create_sets(p_batch_id);
      if l_sets > 0 then
        resolve_sets(p_batch_id);
      end if;
    end if;

    merge_values(p_batch_id, p_overwrite_blanks, l_val_ins, l_val_upd);
    merge_coa   (p_batch_id, p_overwrite_blanks, l_coa_ins, l_coa_upd);

    update xx_coa_map_stg_values set status = 'P' where batch_id = p_batch_id;
    update xx_coa_map_stg_coa    set status = 'P' where batch_id = p_batch_id;

    update xx_coa_map_batch
       set status = 'SUCCESS', error_rows = 0, finished_on = systimestamp,
           sets_created = l_sets,
           values_inserted = l_val_ins, values_updated = l_val_upd,
           coa_inserted = l_coa_ins,    coa_updated = l_coa_upd,
           message = 'Mapping sets created: ' || l_sets
                  || '; values inserted/updated: ' || l_val_ins || '/' || l_val_upd
                  || '; COA rows inserted/updated: ' || l_coa_ins || '/' || l_coa_upd
     where batch_id = p_batch_id;
    commit;
  exception
    when others then
      rollback;
      fail_batch(p_batch_id, 'Unexpected error: ' || sqlerrm || ' ' || dbms_utility.format_error_backtrace);
  end process_batch;

  procedure purge(p_days in number default 90) is
  begin
    delete from xx_coa_map_stg_values
     where batch_id in (select batch_id from xx_coa_map_batch where created_on < systimestamp - p_days);
    delete from xx_coa_map_stg_coa
     where batch_id in (select batch_id from xx_coa_map_batch where created_on < systimestamp - p_days);
    delete from xx_coa_map_batch where created_on < systimestamp - p_days;
    commit;
  end purge;

end xx_coa_map_loader_pkg;
/
show errors package body xx_coa_map_loader_pkg
