"""Local-only video viewer fixture; outbound services are disabled."""
import asyncio
import os
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import FileResponse
import uvicorn
from app.config import settings

ROOT = Path(os.environ['WALKTHROUGH_PREVIEW_DIR'])
VIDEO = Path(os.environ['WALKTHROUGH_PREVIEW_VIDEO'])
PORT = int(os.environ.get('WALKTHROUGH_PREVIEW_PORT', '8767'))
ROOT.mkdir(parents=True, exist_ok=True)
settings.supabase_url = settings.supabase_service_role_key = ''
settings.ops_email = settings.resend_api_key = settings.smtp_host = settings.ops_webhook_url = ''
settings.anthropic_api_key = settings.openai_api_key = ''
settings.storage_dir = str(ROOT / 'data')
settings.flow_token_secret = 'local-video-preview-only'
settings.auth_token = ''
settings.public_base_url = f'http://127.0.0.1:{PORT}'
from app.flow import quote_media as media
from app.flow_quotes import quote_store, QuoteRequestRecord
from app.quote_media_api import router

record = QuoteRequestRecord(id='qr_video_preview', threadId='local-preview', createdAt='2026-11-17',
    homeownerId=str(uuid4()), homeId=str(uuid4()), serviceType='Painting', zip='37129',
    synopsis='Paint the living room walls and keep the wood trim.', measurements={},
    scanMedia=dict(snapshotId='local-video', revision=str(uuid4()), mediaType='walkthrough',
        video={'bucket':'metashape-exports','object':'fixture.mov','mimeType':'video/quicktime'},
        models=[], rooms=[], photos=[], context={}, photosPrepared=True, expiresAt=int(time.time())+3600))
asyncio.run(quote_store.save(record))
async def signed(*args, **kwargs): return '/fixtures/video.mov'
media.supabase_store._signed_storage_url = signed
app = FastAPI()
app.include_router(router, prefix='/api/v1')
@app.get('/fixtures/video.mov')
async def video(): return FileResponse(VIDEO, media_type='video/quicktime')
url = media.link(record)
(ROOT/'url.txt').write_text(url)
print(url, flush=True)
uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='warning')
