const {PGlite} = require(process.env.PGLITE_MODULE || '@electric-sql/pglite');
const fs = require('fs'), path = require('path'), assert = require('node:assert/strict');
(async () => {
 const db = new PGlite();
 await db.exec('create role anon; create role authenticated; create role service_role bypassrls; create table homeowners(id uuid primary key)');
 for (const file of ['20260930163532_crash_safe_scan_uploads.sql','20260930164959_scan_upload_revision_context.sql','20261117174552_walkthrough_project_media.sql']) {
   await db.exec(fs.readFileSync(path.join(__dirname,'../../supabase/migrations',file),'utf8'));
 }
 const owner='11111111-1111-1111-1111-111111111111', foreign='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', home='22222222-2222-2222-2222-222222222222';
 const r1='33333333-3333-3333-3333-333333333333', r2='44444444-4444-4444-4444-444444444444';
 await db.query('insert into homeowners values($1),($2)',[owner,foreign]);
 const index = revision => ({mediaType:'walkthrough',rooms:[],homeModel:{},video:{object:`${owner}/${home}/${revision}.mov`},upload:{ownerId:owner,revision,videoReady:true,modelsReady:false}});
 const publish = async (revision,generation,who=owner,idx=index(revision)) => (await db.query("select walkthrough_upload_transition($1,'publish',$2,$3,$4) as state",[home,revision,who,JSON.stringify({objectPath:idx.video.object,generation,index:idx})])).rows[0].state;
 await db.exec('set role service_role');
 let first = await publish(r1,100);
 assert.equal(first.publishedIndex.upload.videoReady,true);
 assert.equal(first.publishedIndex.upload.modelsReady,false);
 assert.deepEqual((await publish(r1,100)).publishedIndex,first.publishedIndex);
 assert.equal((await publish(r1,100,owner,{...index(r1),video:{...index(r1).video,bytes:55}})).error,'revision_mismatch');
 assert.equal((await publish(r2,200,foreign)).error,'owner_mismatch');
 let second = await publish(r2,200);
 assert.equal(second.publishedIndex.upload.revision,r2);
 assert.equal((await publish(r1,100)).error,'revision_mismatch');
 await db.exec('reset role');
 let grants = (await db.query("select has_function_privilege('anon','walkthrough_upload_transition(uuid,text,uuid,uuid,jsonb)','execute') as anon, has_function_privilege('authenticated','walkthrough_upload_transition(uuid,text,uuid,uuid,jsonb)','execute') as authenticated, prosecdef from pg_proc where proname='walkthrough_upload_transition'")).rows[0];
 assert.deepEqual(grants,{anon:false,authenticated:false,prosecdef:false});
 // Existing scan jobs, even before context finishes, cannot become videos.
 await db.query('delete from scan_upload_heads where home_id=$1',[home]);
 await db.query("select scan_upload_transition($1,'begin',$2,$3,$4)",[home,r1,owner,JSON.stringify({objectPath:'context.zip',generation:100})]);
 assert.equal((await publish(r2,200)).error,'media_type_mismatch');
 // Run the existing lease/revision regressions against the same migrated DB.
 await db.exec(fs.readFileSync(path.join(__dirname,'scan_upload_transitions.sql'),'utf8'));
 await db.close();
 console.log('PASS: video publication, retry identity, replacement/stale fencing, owner isolation, service-only grants, scan compatibility and existing scan transition suite');
})().catch(error => { console.error(error); process.exitCode=1; });
