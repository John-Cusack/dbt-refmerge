-- PostgreSQL 16 synthetic example. Run in a fresh session.
-- EXPLAIN executes the queries; the transaction leaves no benchmark table behind.
begin;
set local work_mem = '4MB';

create temporary table pruning_benchmark as
select
    i as id,
    repeat('x', 128) as payload_0,
    repeat('x', 128) as payload_1,
    repeat('x', 128) as payload_2,
    repeat('x', 128) as payload_3,
    repeat('x', 128) as payload_4,
    repeat('x', 128) as payload_5,
    repeat('x', 128) as payload_6,
    repeat('x', 128) as payload_7
from generate_series(1, 100000) as t(i);
analyze pruning_benchmark;

-- Reused CTE: PostgreSQL normally materializes it. Compare its width/temp writes.
explain (analyze, buffers)
with imported as (select * from pruning_benchmark)
select a.id
from imported as a join imported as b on a.id = b.id
where a.id < 10;

explain (analyze, buffers)
with imported as (select id from pruning_benchmark)
select a.id
from imported as a join imported as b on a.id = b.id
where a.id < 10;

-- Single consumer: the planner can fold the CTE, so the plans may already match.
explain (analyze, buffers)
with imported as (select * from pruning_benchmark)
select id from imported where id < 10;

explain (analyze, buffers)
with imported as (select id from pruning_benchmark)
select id from imported where id < 10;

rollback;
