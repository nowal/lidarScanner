"""Concise, deterministic provider scope shared by email and scan viewer."""
from __future__ import annotations

import re
from typing import Any


FIELD_LABELS = {
    'desired_outcome': 'Requested result', 'target_scope': 'Area',
    'included_surfaces': 'Surfaces', 'extent': 'Extent', 'finish_preference': 'Finish',
    'task_and_result': 'Requested result', 'task_specific_details': 'Issue details',
    'supply_responsibility': 'Parts and materials', 'completion_details': 'Completion',
    'keep_replace_priorities': 'Keep or replace', 'selection_stage': 'Selections',
    'occupancy_constraints': 'Occupancy', 'deadline': 'Timing', 'budget': 'Budget',
    'symptoms': 'Known condition', 'exclusions': 'Not included', 'logistics': 'Access and logistics',
}


def _clean(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip(' -;')


def _key(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', value.casefold())


def _sentence(value: str) -> str:
    value = _clean(value)
    return value + ('' if not value or value[-1] in '.!?' else '.')


def _answer_text(answer: dict) -> str:
    status = answer.get('status')
    if status == 'answered':
        return _clean(answer.get('evidence') or answer.get('value'))
    return {'unknown': 'Homeowner is unsure', 'no_preference': 'No preference',
            'provider_recommendation': 'Provider recommendation requested',
            'provider_review': 'Provider assessment requested',
            'not_applicable': 'Not applicable'}.get(status, _clean(answer.get('evidence')))


def _work_item(item: dict) -> dict:
    title = _clean(item.get('serviceName') or item.get('subtype') or item.get('service') or 'Requested work')
    answers = list(item.get('answers') or [])
    preferred = next((a for a in answers if a.get('field') == 'desired_outcome' and _answer_text(a)), None)
    summary = _answer_text(preferred) if preferred else ''
    details: list[dict[str, str]] = []
    seen = {_key(summary)} if summary else set()
    for answer in answers:
        field, value = _clean(answer.get('field')), _answer_text(answer)
        if not field or not value or field == 'desired_outcome' or value == 'Not applicable':
            continue
        normalized = _key(value)
        if not normalized or normalized in seen:
            continue
        if summary and (normalized in _key(summary) or _key(summary) in normalized):
            continue
        seen.add(normalized)
        details.append({'label': FIELD_LABELS.get(field, field.replace('_', ' ').capitalize()), 'value': value})
    if not summary:
        summary = next((d['value'] for d in details), title)
    return {'title': title, 'summary': _sentence(summary), 'details': details[:6]}


def build(record: Any, scan_facts: list[str] | None = None) -> dict:
    """Return only provider-safe scope already frozen on the quote record."""
    intake = (getattr(record, 'quoteDraft', None) or {}).get('intake') or {}
    items = [_work_item(item) for item in (intake.get('workItems') or [])]
    if not items:
        title = _clean(getattr(record, 'serviceType', None)) or 'Home project'
        summary = _sentence(getattr(record, 'synopsis', None)) or 'Review the saved project request and scan.'
        items = [{'title': title, 'summary': summary, 'details': []}]
    names = [item['title'] for item in items]
    title = names[0] if len(names) == 1 else ' + '.join(names[:3]) + (' + more' if len(names) > 3 else '')
    confirmations = []
    for raw in intake.get('sharedAnswers') or []:
        if raw.get('status') == 'answered' and _answer_text(raw):
            confirmations.append({'label': FIELD_LABELS.get(raw.get('field'), _clean(raw.get('field')).capitalize()),
                                  'value': _answer_text(raw)})
    consultation = intake.get('handoffType') == 'consultation_request'
    return {'title': title, 'summary': ' '.join(item['summary'] for item in items), 'items': items,
            'confirmations': confirmations[:5],
            'confirmationPrompt': ('Confirm the assessment approach, inclusions, schedule, and expected next step.'
                                   if consultation else 'Confirm what is included, the schedule, and the total price.'),
            'requestKind': 'Consultation request' if consultation else 'Quote request',
            'zip': _clean(getattr(record, 'zip', None)), 'room': _clean(getattr(record, 'roomName', None)),
            'scanFacts': [_clean(fact) for fact in (scan_facts or []) if _clean(fact)][:5]}
