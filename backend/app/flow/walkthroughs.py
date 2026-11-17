"""Publish a private walkthrough in the ordinary project/revision lifecycle."""
from fastapi import HTTPException

from ..home_index import HomeIndex
from . import scan_uploads, supabase_store


def ready(index: dict) -> bool:
    upload = index.get('upload') or {}
    if index.get('mediaType') == 'walkthrough':
        return bool(upload.get('videoReady') and (index.get('video') or {}).get('object'))
    return bool(upload.get('modelsReady'))


async def publish(home: str, body, owner: str) -> dict:
    from ..flow_api import _check_scan_object
    extension = '.mov' if body.mimeType == 'video/quicktime' else '.mp4'
    _check_scan_object(home, owner, body.bucket, body.objectPath, extension)
    # Immutable capture names are generated before recording, and retained on retry.
    if body.objectPath.rsplit('/', 1)[-1].lower() != str(body.revision) + extension:
        raise HTTPException(422, 'The video filename must match its capture revision.')
    try:
        size = await supabase_store.stored_object_size(body.bucket, body.objectPath)
    except Exception as exc:
        raise HTTPException(503, 'Could not verify the uploaded video. Please retry.') from exc
    if size != body.bytes:
        raise HTTPException(422, 'The video upload is incomplete.')
    index = HomeIndex([], bundle_id=home, media_type='walkthrough', video={
        'bucket': body.bucket, 'object': body.objectPath, 'bytes': size,
        'mimeType': body.mimeType, 'durationSeconds': body.durationSeconds}, upload={
        'revision': str(body.revision), 'ownerId': owner, 'contextReady': True,
        'videoReady': True, 'modelsReady': False, 'durableV2': True})
    await scan_uploads.transition(home, 'publish', revision=str(body.revision), owner=owner,
        payload={'objectPath': body.objectPath, 'generation': body.generation, 'index': index.to_json()},
        rpc='walkthrough_upload_transition')
    return {'status': 'done', 'revision': str(body.revision), 'mediaType': 'walkthrough'}
