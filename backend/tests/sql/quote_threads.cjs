const { PGlite } = require(process.env.PGLITE_MODULE || '@electric-sql/pglite');
const fs = require('fs'), path = require('path'), assert = require('node:assert/strict');
(async () => {
 const db = new PGlite();
 await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
 create table homeowners(id uuid primary key);
 create table flow_quote_requests(id text primary key,thread_id text,homeowner_id uuid,status text,record jsonb,created_at timestamptz default now());
 create table flow_provider_responses(id uuid primary key,request_id text references flow_quote_requests(id),quote jsonb);
 grant all on homeowners,flow_quote_requests,flow_provider_responses to service_role;`);
 for (const name of ['20261019182934_quote_intake_state.sql','20261030193724_quote_request_lifecycle.sql','20261102171138_quote_threads.sql']) {
  await db.exec(fs.readFileSync(path.join(__dirname,'../../supabase/migrations',name),'utf8'));
 }
 const owner='11111111-1111-1111-1111-111111111111',home='22222222-2222-2222-2222-222222222222';
 const first='33333333-3333-3333-3333-333333333333',second='44444444-4444-4444-4444-444444444444',foreign='55555555-5555-5555-5555-555555555555';
 await db.query('insert into homeowners values($1),($2)',[owner,foreign]);
 await db.exec('set role service_role');
 const state={revision:0,threadVersion:1,transcript:[],receipt:null,pendingSubmission:null};
 const select=async (id,initial=null,who=owner,where=home)=>(await db.query('select select_quote_thread($1,$2,$3,$4) as row',[who,where,id,initial])).rows[0].row;
 const saved=await select(first,state);
 await db.query('insert into flow_quote_requests(id,homeowner_id,status,record) values($1,$2,$3,$4)',['r',owner,'requested',{homeId:home}]);
 await db.query("update quote_intakes set state=jsonb_set(state,'{receipt}',$1),version=version+1 where id=$2",[{quoteRequestId:'r'},first]);
 assert.equal((await select(second,state)).state.receipt,null,'new thread needs no approval');
 assert.equal((await select(first)).state.receipt.quoteRequestId,'r','reopening keeps receipt');
 assert.equal((await select(first,state)).state.receipt.quoteRequestId,'r','retried create never resets history');
 assert.equal((await select(first)).created_at,saved.created_at,'creation date stays stable');
 await assert.rejects(()=>select(first,null,foreign),/Thread not found/);
 await assert.rejects(()=>select(first,state,owner,foreign),/Thread not found/);
 await assert.rejects(()=>select(foreign),/Thread not found/);
 assert.equal((await db.query('select count(*)::int as n from quote_intakes')).rows[0].n,2);
 assert.equal((await db.query('select conversation_id from quote_active_conversations')).rows[0].conversation_id,first);
 await assert.rejects(()=>db.query("update quote_intakes set state=jsonb_set(state,'{receipt}','null') where id=$1",[first]),/immutable|durable receipt/);
 await db.exec('reset role');
 const grants=(await db.query("select has_function_privilege('anon','select_quote_thread(uuid,uuid,uuid,jsonb)','execute') as anon,has_function_privilege('authenticated','select_quote_thread(uuid,uuid,uuid,jsonb)','execute') as auth")).rows[0];
 assert.equal(grants.anon,false);assert.equal(grants.auth,false);
 console.log('PASS: saved thread SQL, idempotent create, history, dates, receipt immutability, owner/home isolation and private grants.');
 await db.close();
})().catch(e=>{console.error(e);process.exitCode=1;});
