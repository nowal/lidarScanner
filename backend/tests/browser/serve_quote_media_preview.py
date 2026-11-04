"""Isolated synthetic room for browser checks. Uses no production credentials."""
import asyncio, io, json, time, os, math
from pathlib import Path
from PIL import Image, ImageDraw
from pxr import Usd, UsdGeom, Gf
from fastapi import FastAPI
from fastapi.responses import FileResponse
import uvicorn
from app.config import settings
settings.supabase_url = settings.supabase_service_role_key = ''
ROOT = Path(os.environ.get('PROVIDER_PREVIEW_DIR', '/tmp/takeshape-provider-preview'))
PORT = int(os.environ.get('PROVIDER_PREVIEW_PORT', '8765'))
settings.storage_dir = str(ROOT / 'data')
settings.ops_email = settings.resend_api_key = settings.smtp_host = ''
settings.flow_token_secret = 'local-preview-only'
settings.auth_token = ''
settings.public_base_url = f'http://127.0.0.1:{PORT}'
from app.flow import quote_media as media
from app.flow_quotes import quote_store, QuoteRequestRecord
from app.flow.usdz_to_glb import convert
from app.quote_media_api import router
from app.flow_api import get_quote_request
ROOT.mkdir(parents=True, exist_ok=True)
stage=Usd.Stage.CreateNew(str(ROOT/'room.usda'))
UsdGeom.SetStageMetersPerUnit(stage, 1)
UsdGeom.SetStageUpAxis(stage, 'Y')
# Three open room surfaces, rendered in the same Y-up metre space as cameras.
for name, points, color in [
 ('floor',[(-3,0,-2),(3,0,-2),(3,0,2),(-3,0,2)],(.65,.57,.44)),
 ('back',[(-3,0,-2),(-3,2.7,-2),(3,2.7,-2),(3,0,-2)],(.86,.89,.83)),
 ('left',[(-3,0,2),(-3,2.7,2),(-3,2.7,-2),(-3,0,-2)],(.8,.86,.89))]:
 mesh=UsdGeom.Mesh.Define(stage, '/'+name)
 mesh.CreatePointsAttr(points); mesh.CreateFaceVertexCountsAttr([3,3]); mesh.CreateFaceVertexIndicesAttr([0,1,2,0,2,3])
 mesh.CreateSubdivisionSchemeAttr('none'); mesh.CreateDisplayColorAttr([color])
 mesh.CreateDoubleSidedAttr(True)
stage.GetRootLayer().Save()
convert(ROOT/'room.usda',ROOT/'model.glb')
for n in range(3):
 im=Image.new('RGB',(900,600),('#bfcabd','#d8c3a0','#aebdc7')[n]); d=ImageDraw.Draw(im)
 d.rectangle((80,80,820,510),outline='#ffffff',width=8); d.rectangle((570,150,730,360),fill='#eff6ff',outline='#9ba8af',width=5)
 d.text((120,125),f'Sample scan photo {n+1}',fill='#263e33',font_size=35)
 im.save(ROOT/f'photo-{n}.jpg')
Image.new('RGB',(500,650),'white').save(ROOT/'estimate.pdf','PDF')
previous_url = ROOT / 'url.txt'
exp = int(previous_url.read_text().split('/')[-2]) if previous_url.exists() else int(time.time()) + 3600
if exp <= time.time(): exp = int(time.time()) + 3600
poses = [([0,1.3,1],[0,0,-1]),([1.7,1.3,.4],[-1,0,-.4]),([-1.4,1.3,.4],[1,0,-.4])]
for n in range(3, 240):
 angle = n * 2.39996
 poses.append(([2.5 * math.cos(angle), 1.1 + (n % 7) * .08, 1.5 * math.sin(angle)], [-math.cos(angle), 0, -math.sin(angle)]))
# Include upward/downward cameras so frustum basis construction is exercised.
poses[3] = (poses[3][0], [0, 1, 0])
poses[4] = (poses[4][0], [0, -1, 0])
rec=QuoteRequestRecord(id='qr_local_preview',createdAt='2026-10-13',threadId='preview',homeId='25d800a5-d2ba-4ec0-9dd2-7509a7d28ec9',scanMedia={
 'snapshotId':'preview','revision':'sample-scan','ownerId':'cdeca749-2170-4355-b92f-746e39429c61','homeId':'25d800a5-d2ba-4ec0-9dd2-7509a7d28ec9','expiresAt':exp,
 'models':[{'key':key,'label':label,'bucket':'local','object':'model.glb'} for key,label in [('home','Sample whole home'),('room-1','Sample living room')]],
 'photos':[{'label':f'Sample scan · view {n+1}','roomKey':'room-1' if n<120 else 'room-2','object':f'photo-{n%3}.jpg','thumbnail':f'photo-{n%3}.jpg','camera':{'position':pos,'direction':direction}} for n,(pos,direction) in enumerate(poses)] + [
 {'label':'Homeowner detail','supplemental':True,'object':'photo-0.jpg','thumbnail':'photo-0.jpg','camera':None},
 {'label':'Older unposed scan photo','roomKey':'room-1','object':'photo-1.jpg','thumbnail':'photo-1.jpg','camera':None}]})
asyncio.run(quote_store.save(rec))
async def status(*a):return {'status':'ready'}
async def signed(bucket,key,expires_in=300):return '/fixtures/'+('model.glb' if key.endswith('.glb') else key.rsplit('/',1)[-1])
async def put(key,data,content_type,**kw):(ROOT/key.rsplit('/',1)[-1]).write_bytes(data)
media.conversion_status=status
media.supabase_store._signed_storage_url=signed
media.put_bytes=put
# Render the actual email in the isolated fixture, without sending anything.
rec.serviceType = 'Painting'
rec.zip = '37129'
rec.firstName = 'Quentin'
rec.roomName = 'Bedroom'
rec.scopeIntent = 'single_room'
rec.synopsis = 'Quentin is interested in painting the bedroom. Walls and trim were discussed, with options for a fresh neutral finish.'
rec.scopeOptions = ['Walls and trim']
rec.measurements = {'floorAreaSquareFeet': 214, 'paintableWallSquareFeet': 410, 'windowCount': 2}
from app.flow.ops_email import build_ops_email_html
(ROOT / 'email.html').write_text(build_ops_email_html(rec, [], None))
app=FastAPI()
app.include_router(router,prefix='/api/v1')
app.get('/api/v1/ai/quote-requests/{request_id}')(get_quote_request)
@app.get('/fixtures/{name}')
async def fixture(name:str):return FileResponse(ROOT/name)
url=f'http://127.0.0.1:{PORT}/api/v1/quote-media/{rec.id}/{exp}/{media.signature(rec,exp)}'
(ROOT/'url.txt').write_text(url)
print(url,flush=True)
uvicorn.run(app,host='127.0.0.1',port=PORT,log_level='warning')
