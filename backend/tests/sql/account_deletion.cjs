const {PGlite} = require(process.env.PGLITE_MODULE || '@electric-sql/pglite');
const fs = require('fs'), path = require('path'), assert = require('node:assert/strict');
(async () => {
 const db = new PGlite();
 await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
 create schema storage;
 create table storage.objects(bucket_id text, name text, owner_id text);
 create table homeowners(id uuid primary key, auth_user_id uuid);
 create table providers(id uuid primary key, auth_user_id uuid);
 create table provider_profiles(auth_user_id uuid, provider_id uuid references providers on delete cascade);
 create table scan_upload_heads(home_id uuid primary key,owner_id uuid references homeowners on delete cascade);
 create table quote_intakes(home_id uuid,owner_id uuid references homeowners on delete cascade);
 create table flow_quote_requests(id text,thread_id text,homeowner_id uuid references homeowners on delete cascade,record jsonb);
 create table flow_states(thread_id text,homeowner_id uuid references homeowners on delete cascade);
 create table quote_requests(id uuid,homeowner_id uuid references homeowners on delete cascade,provider_id uuid references providers on delete cascade);
 grant usage on schema public,storage to service_role;
 grant all on all tables in schema public,storage to service_role;`);
 await db.exec(fs.readFileSync(path.join(__dirname,'../../supabase/migrations/20261119162953_account_deletion.sql'),'utf8'));
 const user='11111111-1111-1111-1111-111111111111', owner='22222222-2222-2222-2222-222222222222';
 const other='33333333-3333-3333-3333-333333333333', home='44444444-4444-4444-4444-444444444444';
 const quote='55555555-5555-5555-5555-555555555555', provider='66666666-6666-6666-6666-666666666666';
 await db.query('insert into homeowners values($1,$2),($3,$3)',[owner,user,other]);
 await db.query('insert into providers values($1,$2)',[provider,user]);
 await db.query('insert into scan_upload_heads values($1,$2)',[home,owner]);
 await db.query('insert into quote_requests values($1,$2,null)',[quote,owner]);
 await db.query('insert into flow_quote_requests values($1,$2,$3,$4)',['request','thread',owner,JSON.stringify({homeId:home})]);
 for (const [bucket,name,who] of [
  ['metashape-exports',`${owner}/${home}/video.mov`,user],
  ['metashape-exports',`${owner}/${home}/quote-intake/photo.jpg`,null],
  ['home-assets',`home-indexes/${home}.json`,null],
  ['home-assets',`home-models/${home}/room-1.usdz`,null],
  ['quote-documents',`${quote}/quote.pdf`,other],
  ['provider-referral-uploads',`${provider}/import.csv`,null],
  ['metashape-exports',`${other}/${home}/foreign.mov`,other],
  ['metashape-exports',`${owner}-suffix/private.mov`,other]]) {
   await db.query('insert into storage.objects values($1,$2,$3)',[bucket,name,who]);
 }
 const manifest=async()=>(await db.query('select account_deletion_manifest($1) as m',[user])).rows[0].m;
 await db.exec('set role service_role');
 const plan=await manifest(); assert.equal(plan.objects.length,6); assert.deepEqual(plan.owners,[owner]);
 assert(plan.objects.every(o=>!o.path.endsWith('foreign.mov')&&!o.path.includes('-suffix/')));
 await assert.rejects(db.query('select account_deletion_remove_profiles($1)',[user]), /files must be removed first/);
 // Emulate Storage API removals only inside this isolated test database.
 for(const o of plan.objects) await db.query('delete from storage.objects where bucket_id=$1 and name=$2',[o.bucket,o.path]);
 await db.query('select account_deletion_remove_profiles($1)',[user]);
 assert.equal((await db.query('select count(*)::int as n from homeowners')).rows[0].n,1);
 assert.equal((await db.query('select count(*)::int as n from providers')).rows[0].n,0);
 assert.equal((await db.query('select count(*)::int as n from flow_quote_requests')).rows[0].n,0);
 // A retry after Auth failure retains the removed profile's ownership roots.
 await db.query('insert into storage.objects values($1,$2,null)',['metashape-exports',`${owner}/${home}/late.mov`]);
 assert.equal((await manifest()).objects.length,1);
 await db.exec('reset role');
 const grants=(await db.query(`select
 has_function_privilege('anon','account_deletion_manifest(uuid)','execute') as anon,
 has_function_privilege('authenticated','account_deletion_remove_profiles(uuid)','execute') as authenticated,
 has_table_privilege('authenticated','account_deletions','select') as receipt_access,
 (select bool_or(prosecdef) from pg_proc where proname like 'account_deletion_%') as definer`)).rows[0];
 assert.deepEqual(grants,{anon:false,authenticated:false,receipt_access:false,definer:false});
 await db.close();
 console.log('PASS: ownership isolation, all media types, file-before-profile guard, profile cascades, retry recovery, service-only permissions');
})().catch(error=>{console.error(error);process.exitCode=1});
