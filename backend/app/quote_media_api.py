"""Expiring scan access and PDF responses scoped to the emailed request."""
import hmac
import math
import re
from uuid import UUID
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Query, Header
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse

from .flow import quote_media as media, supabase_store
from .flow import provider_responses
from .flow_quotes import QuoteDocument, ReturnedQuote, normalize_provider_email

router = APIRouter()
ROOT = '/quote-media/{request_id}/{exp}/{sig}'
HEADERS = {'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff', 'X-Robots-Tag': 'noindex, nofollow'}
STATIC = Path(__file__).parent / 'static' / 'quote-media'


@router.get('/quote-media-assets/{filename}')
async def asset(filename: str):
    if filename not in ('viewer.js', 'viewer.css', 'measurements.js', 'model-viewer.min.js'):
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
        models.append({'label': m['label'], 'key': m['key'], 'status': state['status'], 'index': n})
    photos = await media.photos_with_camera(scan)
    return JSONResponse({'models': models, 'photos': [{'label': p['label'], 'index': n,
                         'roomKey': p.get('roomKey'), 'camera': p.get('camera'),
                         'supplemental': bool(p.get('supplemental'))} for n, p in enumerate(photos)],
                         'revision': scan['revision'][:8], 'expiresAt': exp,
                         'canRespond': record.status != 'closed', 'uploadToken': upload_token(sig),
                         'maxPdfBytes': provider_responses.MAX_PDF_BYTES}, headers=HEADERS)


def upload_token(signature: str) -> str:
    # A separate intent token prevents cross-site form posts using the URL alone.
    return hmac.new(signature.encode(), b'provider-pdf-response-v1', 'sha256').hexdigest()


@router.post(ROOT + '/responses', status_code=201)
async def upload_response(request_id: str, exp: int, sig: str, request: Request,
                          submission_id: UUID, provider_name: str = Query(min_length=1, max_length=200),
                          file_name: str = Query(min_length=1, max_length=200),
                          price_usd: float | None = Query(default=None, ge=0),
                          provider_email: str | None = Query(default=None, max_length=254),
                          x_upload_token: str | None = Header(default=None)):
    record = await media.authorized(request_id, exp, sig)
    if not hmac.compare_digest(x_upload_token or '', upload_token(sig)):
        raise HTTPException(403, 'Reload the viewer before submitting your quote.')
    provider_name = ' '.join(provider_name.split())
    if not provider_name:
        raise HTTPException(422, 'Enter your business name.')
    # Older open viewers can finish/retry priced submissions. New viewers keep
    # pricing in the PDF; an absent amount must stay unknown, never become $0.
    if price_usd is not None and not math.isfinite(price_usd):
        raise HTTPException(422, 'The supplied price is invalid.')
    try:
        provider_email = normalize_provider_email(provider_email)
    except ValueError as exc:
        raise HTTPException(422, 'Enter one valid business email address.') from exc
    if request.headers.get('content-type', '').split(';')[0].lower() != 'application/pdf':
        raise HTTPException(415, 'Choose a PDF quote.')
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > provider_responses.MAX_PDF_BYTES:
            raise HTTPException(413, 'The PDF must be 25 MB or smaller.')
        data.extend(chunk)
    data = bytes(data)
    if not data.startswith(b'%PDF-') or b'%%EOF' not in data[-1024:]:
        raise HTTPException(422, 'This file does not appear to be a complete PDF. Export it as a PDF and retry.')
    receipt_id = str(submission_id)
    fingerprint = provider_responses.fingerprint(data, provider_name, price_usd, provider_email=provider_email)
    existing = await provider_responses.existing(request_id, receipt_id, fingerprint)
    if existing:
        return JSONResponse({'quoteId': receipt_id, 'status': 'received'}, headers=HEADERS)
    if record.status == 'closed':
        raise HTTPException(409, 'This quote request is closed.')
    name = re.sub(r'[^\w .()-]', '', file_name.rsplit('/', 1)[-1]).strip('. ')[:180] or 'Quote.pdf'
    if not name.lower().endswith('.pdf'): name += '.pdf'
    path = media.prefix(record.scanMedia) + f'quote-response-{receipt_id}-{fingerprint}.pdf'
    try:
        await media.put_bytes(path, data, 'application/pdf')
    except Exception as exc:
        raise HTTPException(503, 'The PDF could not be uploaded. Please retry.') from exc
    quote = ReturnedQuote(id=receipt_id, providerName=provider_name, providerEmail=provider_email, priceUsd=price_usd,
        document=QuoteDocument(fileName=name, byteCount=len(data), storageBucket=media.BUCKET, storagePath=path))
    await provider_responses.save(request_id, receipt_id, fingerprint, quote)
    return JSONResponse({'quoteId': receipt_id, 'status': 'received'}, status_code=201, headers=HEADERS)


@router.get(ROOT + '/photos/{number}/{size}')
async def photo(request_id: str, exp: int, sig: str, number: int, size: str):
    record = await media.authorized(request_id, exp, sig)
    photos = record.scanMedia['photos']
    if number < 0 or number >= len(photos) or size not in ('full', 'thumb'): raise HTTPException(404)
    key = await media.photo_object(record.scanMedia, photos[number], size)
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
