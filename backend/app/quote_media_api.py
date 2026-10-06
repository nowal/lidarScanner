"""Read-only, expiring capabilities for a quote's immutable scan snapshot."""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse

from .flow import quote_media as media, supabase_store

router = APIRouter()
ROOT = '/quote-media/{request_id}/{exp}/{sig}'
HEADERS = {'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff', 'X-Robots-Tag': 'noindex, nofollow'}
STATIC = Path(__file__).parent / 'static' / 'quote-media'


@router.get('/quote-media-assets/{filename}')
async def asset(filename: str):
    if filename not in ('viewer.js', 'viewer.css', 'model-viewer.min.js'):
        raise HTTPException(404)
    return FileResponse(STATIC / filename, headers={'X-Content-Type-Options': 'nosniff'})


@router.get(ROOT, response_class=HTMLResponse)
async def viewer(request_id: str, exp: int, sig: str):
    await media.authorized(request_id, exp, sig)
    page = (STATIC / 'viewer.html').read_text()
    return HTMLResponse(page, headers=dict(HEADERS, **{'Content-Security-Policy':
        "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' https: blob:; "
        "connect-src 'self' https: blob:; worker-src blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"}))


@router.get(ROOT + '/status')
async def status(request_id: str, exp: int, sig: str):
    record = await media.authorized(request_id, exp, sig)
    scan = record.scanMedia
    models = []
    for n, m in enumerate(scan['models']):
        state = await media.conversion_status(scan, m)
        models.append({'label': m['label'], 'status': state['status'], 'index': n})
    return JSONResponse({'models': models, 'photos': [{'label': p['label'], 'index': n} for n, p in enumerate(scan['photos'])],
                         'revision': scan['revision'][:8], 'expiresAt': exp}, headers=HEADERS)


@router.get(ROOT + '/photos/{number}/{size}')
async def photo(request_id: str, exp: int, sig: str, number: int, size: str):
    record = await media.authorized(request_id, exp, sig)
    photos = record.scanMedia['photos']
    if number < 0 or number >= len(photos) or size not in ('full', 'thumb'): raise HTTPException(404)
    key = photos[number]['object' if size == 'full' else 'thumbnail']
    url = await supabase_store._signed_storage_url(media.BUCKET, key, expires_in=min(300, exp - int(media.time.time())))
    if not url: raise HTTPException(404, 'This scan photo is unavailable')
    return RedirectResponse(url, headers=HEADERS)


@router.get(ROOT + '/models/{number}/{format}')
async def model(request_id: str, exp: int, sig: str, number: int, format: str):
    record = await media.authorized(request_id, exp, sig)
    models = record.scanMedia['models']
    if number < 0 or number >= len(models) or format not in ('usdz', 'glb'): raise HTTPException(404)
    m = models[number]
    if format == 'glb':
        state = await media.conversion_status(record.scanMedia, m)
        if state['status'] != 'ready': raise HTTPException(409, 'The desktop model is still preparing or unavailable')
        bucket, path = media.BUCKET, media.derivative_path(record.scanMedia, m)
    else:
        bucket, path = m['bucket'], m['object']
    url = await supabase_store._signed_storage_url(bucket, path, expires_in=min(300, exp - int(media.time.time())))
    if not url: raise HTTPException(404, 'This model is unavailable')
    return RedirectResponse(url, headers=HEADERS)
