"""Homeowner approval artifacts. Private documents use quote-scoped capabilities."""
import hashlib
import hmac
from datetime import datetime

from fastapi import HTTPException, Request

from ..config import settings
from ..flow_quotes import QuoteDocument
from . import quote_media, provider_responses


def signature(record, quote, exp: int) -> str:
    secret = settings.flow_token_secret or settings.auth_token
    if not secret:
        raise ValueError('Private approval signing is not configured')
    path = quote.signedDocument.storagePath if quote.signedDocument else ''
    payload = f'quote-approval-v1:{record.id}:{quote.id}:{path}:{exp}'
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def document_link(record, quote) -> str | None:
    if not quote.signedDocument or not settings.public_base_url:
        return None
    exp = int(datetime.fromisoformat(quote.decidedAt or quote.signedDocument.uploadedAt).timestamp()) + 30 * 86400
    return (f'{settings.public_base_url.rstrip("/")}{settings.api_prefix}/quote-approvals/'
            f'{record.id}/{quote.id}/{exp}/{signature(record, quote, exp)}')


async def save_signed_pdf(record, quote, request: Request | bytes) -> QuoteDocument:
    if not quote.document:
        raise HTTPException(422, 'This quote has no PDF to sign. Choose a callback instead.')
    data = bytearray()
    if isinstance(request, bytes):
        data.extend(request)
    else:
        async for chunk in request.stream():
            if len(data) + len(chunk) > provider_responses.MAX_PDF_BYTES:
                raise HTTPException(413, 'The signed PDF must be 25 MB or smaller.')
            data.extend(chunk)
    if len(data) > provider_responses.MAX_PDF_BYTES:
        raise HTTPException(413, 'The signed PDF must be 25 MB or smaller.')
    if not data.startswith(b'%PDF-') or b'%%EOF' not in data[-1024:]:
        raise HTTPException(422, 'The signed PDF is incomplete. Please try again.')
    # Hashes make retries address the same object without trusting client filenames.
    digest = hashlib.sha256(data).hexdigest()
    request_key = hashlib.sha256(record.id.encode()).hexdigest()
    quote_key = hashlib.sha256(quote.id.encode()).hexdigest()
    # Prefer the scan folder so deleting the home also removes approval artifacts.
    folder = quote_media.prefix(record.scanMedia) if record.scanMedia.get('ownerId') and record.scanMedia.get('homeId') else 'quote-approvals/'
    path = f'{folder}signed-{request_key}-{quote_key}-{digest}.pdf'
    try:
        await quote_media.put_bytes(path, bytes(data), 'application/pdf')
    except Exception as exc:
        raise HTTPException(503, 'The signed PDF could not be saved. Please try again.') from exc
    return QuoteDocument(fileName='Signed ' + quote.document.fileName[:190], byteCount=len(data),
                         storageBucket=quote_media.BUCKET, storagePath=path)
