"""Strict, non-executable Markdown/YAML service document loader."""
from __future__ import annotations

import hashlib
import copy
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .catalog import KNOWN_SERVICE_TYPES

DOCS = Path(__file__).resolve().parents[2] / 'docs'
RUBRIC_PATH = DOCS / 'QUOTE_INTAKE_RUBRICS.md'
PROMPT_PATH = DOCS / 'QUOTE_INTAKE_SYSTEM_PROMPT.md'
PROMPT_VERSION = 'quote-intake-v1.15'
Status = Literal['answered', 'unknown', 'no_preference', 'provider_recommendation', 'not_applicable', 'provider_review']
UnknownOutcome = Literal['ready', 'needs_provider_review', 'consultation_required']
DecisionCategory = Literal['scope', 'service_level', 'preference', 'design', 'logistics', 'timing', 'routing']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Condition(StrictModel):
    field: str
    equals: str | None = None
    one_of: list[str] | None = None
    and_field: str | None = None
    and_equals: str | None = None

    @model_validator(mode='after')
    def one_operator(self):
        if (self.equals is None) == (self.one_of is None) or self.one_of == []:
            raise ValueError('when needs exactly one of equals or nonempty one_of')
        if (self.and_field is None) != (self.and_equals is None):
            raise ValueError('when secondary condition needs both and_field and and_equals')
        return self


class Decision(StrictModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
    priority: int = Field(ge=1)
    question: str = Field(min_length=1)
    category: DecisionCategory
    unknown_outcome: UnknownOutcome
    asked_to: Literal['homeowner'] = 'homeowner'
    required_for_quote: bool = True
    can_provider_derive_from_model: Literal[False] = False
    defer: list[Status]
    values: list[str] | None = None
    when: Condition | None = None

    @model_validator(mode='after')
    def safe_decision(self):
        if any(s in self.defer for s in ('answered', 'provider_review')):
            raise ValueError('defer must describe a homeowner uncertainty/preference')
        if len(set(self.defer)) != len(self.defer) or self.values == []:
            raise ValueError('duplicate defer or empty values')
        if self.values and len(set(self.values)) != len(self.values):
            raise ValueError('duplicate values')
        if re.search(r'\b(dimensions|square footage|ceiling height|window count|phone number|street address)\b', re.sub(r'No street address needed[^?]*', '', self.question), re.I):
            raise ValueError('physical measurements and contact details cannot be homeowner decision questions')
        return self


class RequiredField(StrictModel):
    id: str
    question: str = Field(min_length=1)
    rule: str
    category: DecisionCategory
    unknown_outcome: UnknownOutcome
    asked_to: Literal['homeowner'] = 'homeowner'
    required_for_quote: Literal[True] = True
    can_provider_derive_from_model: Literal[False] = False


class Common(StrictModel):
    required: list[RequiredField] = Field(min_length=1)
    shared_optional: list[str]
    provider_review: list[str]
    provider_model_review: list[str]
    provider_technical_review: list[str]
    readiness: list[str]


class CommonBlock(StrictModel):
    rubric_version: str = Field(min_length=1)
    common: Common


class Service(StrictModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
    catalog_service: str | None
    decisions: list[Decision] = Field(min_length=1)
    optional: list[str]
    provider_review: list[str]
    provider_model_review: list[str] = []
    provider_technical_review: list[str] = []
    quote_options: list[str] = []
    consultation_before_quote: bool = False
    consultation_when: Condition | None = None


class Snapshot(StrictModel):
    rubric_version: str
    content_hash: str
    common: Common
    services: list[Service]

    @model_validator(mode='before')
    @classmethod
    def read_original_pinned_snapshot(cls, value):
        # Existing v1 conversations retain their questions, prompt and content
        # hash. New metadata only supplies the original provider-review behavior.
        # parse_document still requires all fields in newly authored rubrics.
        if not isinstance(value, dict) or value.get('rubric_version') != 'quote-intake-v1':
            return value
        value = copy.deepcopy(value)
        common = value.get('common', {})
        common.setdefault('provider_model_review', [])
        common.setdefault('provider_technical_review', [])
        for field in common.get('required', []):
            field.setdefault('category', 'scope')
            field.setdefault('unknown_outcome', 'needs_provider_review')
        for service in value.get('services', []):
            for field in service.get('decisions', []):
                field.setdefault('category', 'scope')
                field.setdefault('unknown_outcome', 'needs_provider_review')
        return value

    def service(self, identifier: str) -> Service:
        return next(s for s in self.services if s.id == identifier)


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f'duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_document(text: str) -> Snapshot:
    blocks = re.findall(r'^```yaml\s*\n(.*?)^```\s*$', text, re.M | re.S)
    if not blocks:
        raise ValueError('No YAML rubric blocks found')
    common = None
    services = []
    for number, block in enumerate(blocks, 1):
        try:
            data = yaml.load(block, Loader=UniqueLoader)
            if not isinstance(data, dict):
                raise ValueError('block must be a mapping')
            if 'common' in data:
                if common is not None:
                    raise ValueError('duplicate common block')
                common = CommonBlock.model_validate(data)
            else:
                services.append(Service.model_validate(data))
        except (ValueError, yaml.YAMLError) as exc:
            raise ValueError(f'Rubric YAML block {number}: {exc}') from exc
    if common is None:
        raise ValueError('Missing common block')
    ids = [s.id for s in services]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate service id')
    common_ids = [f.id for f in common.common.required]
    if len(common_ids) != len(set(common_ids)) or set(common_ids) != {'target_scope', 'desired_outcome'}:
        raise ValueError('Common required fields must be unique target_scope and desired_outcome')
    mapped = [s.catalog_service for s in services if s.catalog_service is not None]
    if len(mapped) != len(set(mapped)) or set(mapped) != set(KNOWN_SERVICE_TYPES):
        raise ValueError(f'Catalog mappings must cover each supported label exactly once: {KNOWN_SERVICE_TYPES}')
    if 'generic_assessment' not in ids or next(s for s in services if s.id == 'generic_assessment').catalog_service is not None:
        raise ValueError('An unmapped generic_assessment rubric is required')
    for service in services:
        fields = {f.id: f for f in service.decisions}
        if len(fields) != len(service.decisions) or set(fields) & set(common_ids):
            raise ValueError(f'{service.id}: duplicate field id')
        for field in service.decisions:
            if not field.when:
                continue
            parent = fields.get(field.when.field)
            values = [field.when.equals] if field.when.equals is not None else field.when.one_of
            if not parent or parent.id == field.id or not parent.values or not set(values) <= set(parent.values):
                raise ValueError(f'{service.id}.{field.id}: when must reference another enumerated field and valid values')
            if field.when.and_field:
                second = fields.get(field.when.and_field)
                if (not second or second.id == field.id or not second.values
                        or field.when.and_equals not in second.values):
                    raise ValueError(f'{service.id}.{field.id}: secondary when must reference another enumerated field and valid value')
            seen = {field.id}
            while parent and parent.when:
                if parent.id in seen:
                    raise ValueError(f'{service.id}.{field.id}: cyclic condition')
                seen.add(parent.id)
                parent = fields.get(parent.when.field)
        if service.consultation_when:
            condition = service.consultation_when
            parent = fields.get(condition.field)
            values = [condition.equals] if condition.equals is not None else condition.one_of
            if not parent or not parent.values or not set(values) <= set(parent.values):
                raise ValueError(f'{service.id}: consultation_when must reference an enumerated decision')
    return Snapshot(rubric_version=common.rubric_version, content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    common=common.common, services=services)


@lru_cache(maxsize=1)
def current_snapshot() -> Snapshot:
    return parse_document(RUBRIC_PATH.read_text())


@lru_cache(maxsize=1)
def current_prompt() -> str:
    text = PROMPT_PATH.read_text()
    marker = '## Runtime instructions\n'
    if marker not in text:
        raise ValueError('System prompt is missing Runtime instructions')
    return text.split(marker, 1)[1].strip()


if __name__ == '__main__':
    snapshot = current_snapshot()
    print(f'Valid: {snapshot.rubric_version}, {len(snapshot.services)} services, sha256={snapshot.content_hash}')
    print(f'Prompt: {PROMPT_VERSION}, sha256={hashlib.sha256(current_prompt().encode()).hexdigest()}')
