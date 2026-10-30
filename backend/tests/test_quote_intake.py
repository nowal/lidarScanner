"""Deterministic intake tests. Model extraction and all delivery are mocked."""
import asyncio
import base64
import copy
import io
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from fastapi import HTTPException
from app.config import settings
from app.flow import ops_email
from app.flow_quotes import quote_store
from app.quote_intake import api, conversation, engine, provider, rubrics, store
from app.quote_intake.schema import Handoff, Result, TurnRequest
from app.quote_intake.zip_codes import normalize_zip
from test_direct_quote_requests import direct


@pytest.fixture(autouse=True)
def isolated_rate_windows(monkeypatch):
    from app import armor
    monkeypatch.setattr(armor, '_identity_window', armor.SlidingWindow())
    monkeypatch.setattr(armor, '_address_window', armor.SlidingWindow())


PAINT = 'Paint the living room walls only, full repaint, you choose the finish, ZIP 01234.'


def state_for(text=PAINT):
    state = engine.initial_state()
    state['transcript'] = [dict(id='turn-1', role='homeowner', text=text)]
    state['pendingTurn'] = dict(clientTurnId='turn-1')
    return state


def answer(field, value, evidence, status='answered', source='turn-1'):
    return dict(field=field, value=value, status=status, sourceTurnId=source, evidence=evidence)


def item(rubric='painting', answers=None, subtype=None, assessment=False, id='work'):
    service = rubrics.current_snapshot().service(rubric)
    return dict(id=id, rubricId=rubric, service=service.catalog_service,
        serviceName=service.catalog_service or 'Plumbing', subtype=subtype, assessment=assessment,
        answers=answers or [], providerReview=[])


def painting_answers():
    return [answer('desired_outcome', 'full repaint', 'full repaint'),
        answer('target_scope', 'living room', 'living room'),
        answer('included_surfaces', 'walls only', 'walls only'),
        answer('extent', 'full repaint', 'full repaint'),
        answer('finish_preference', None, 'you choose the finish', 'provider_recommendation')]


def result(items=None, **overrides):
    data = dict(displayText='Your scope is saved.', serviceCandidates=['Painting'],
        workItems=items if items is not None else [item(answers=painting_answers())], removedWorkItems=[],
        answerUpdates=[], zipUpdates=[dict(role='project', raw='01234', sourceTurnId='turn-1')],
        nextQuestion=[], deferred=[], quoteBrief='Paint the living room walls only. Finish recommendation requested.',
        action='ready', intent='request', intentSourceTurnId='turn-1', intentEvidence='Paint')
    data.update(overrides)
    return Result.model_validate(data)


def test_shared_updates_reject_service_preferences_at_schema_boundary():
    data = result().model_dump()
    data['answerUpdates'] = [answer('room_colors', 'light blue, hunter green, and pink',
                                    'light blue color')]
    with pytest.raises(ValueError):
        Result.model_validate(data)


def wire(home, text=PAINT, revision=0, conversation=None):
    return dict(conversationId=conversation or str(uuid4()), homeId=home, clientTurnId=str(uuid4()),
                expectedRevision=revision, text=text, timeZone='America/New_York')


def mapped_reply(payload, template=None):
    payload = json.loads(payload)
    latest = next(t for t in reversed(payload['transcript']) if t['role'] == 'homeowner')
    data = (template or result()).model_dump()
    raw = json.dumps(data).replace('turn-1', latest['id'])
    return Result.model_validate_json(raw)


def test_complete_request_uses_all_answers_and_provider_package():
    state = engine.apply_result(state_for(), result())
    assert state['ready'] and state['projectZip'] == '01234'
    assert state['zipAnswers'][0]['sourceTurnId'] == 'turn-1'
    assert state['result']['nextQuestion'] == []
    package = engine.provider_package(state)
    assert PAINT in package['description']
    assert 'provider recommendation requested' in package['description']
    assert 'whole home' not in package['description'].lower()
    assert package['details']['rubricHash'] == state['rubric']['content_hash']


def test_ready_reassures_homeowner_about_deferred_choices():
    state = engine.apply_result(state_for(), result())
    assert state['ready']
    assert 'don’t have to make those choices alone' in state['result']['displayText']
    assert 'quote consultation' in state['result']['displayText']
    assert 'you’ll be contacted about your quote' in state['result']['displayText']


def test_ready_followup_answers_completion_question_without_repeating_confirmation():
    state = engine.apply_result(state_for(), result())
    state['transcript'].append(dict(id='turn-2', role='assistant', text=state['result']['displayText']))
    state['transcript'].append(dict(id='turn-3', role='homeowner', text='Sounds good, do you need anything else from me?'))
    state['pendingTurn'] = dict(clientTurnId='turn-3')
    followup = result(zipUpdates=[], intentSourceTurnId='turn-3', intentEvidence='do you need anything else')
    updated = engine.apply_result(state, followup)
    assert updated['result']['displayText'].startswith('No—that’s everything I need from you for now.')
    assert updated['result']['displayText'] != state['result']['displayText']
    assert 'quote consultation' in updated['result']['displayText']


def test_ready_followup_explains_next_steps_without_repeating_confirmation():
    state = engine.apply_result(state_for(), result())
    previous = state['result']['displayText']
    state['transcript'].append(dict(id='turn-2', role='assistant', text=previous))
    state['transcript'].append(dict(id='turn-3', role='homeowner', text='Sounds good. What happens now?'))
    state['pendingTurn'] = dict(clientTurnId='turn-3')
    followup = result(zipUpdates=[], intentSourceTurnId='turn-3', intentEvidence='What happens now')
    updated = engine.apply_result(state, followup)
    assert updated['result']['displayText'].startswith('Next, TakeShape will match')
    assert 'you’ll be contacted about your quote' in updated['result']['displayText']
    assert updated['result']['displayText'] != previous


def test_gutter_replacement_requires_high_value_homeowner_decisions_only_when_applicable():
    gutter = item('roofing_siding', [
        answer('target_scope', 'whole house', 'whole house'),
        answer('desired_outcome', 'replace old damaged gutters', 'replace old damaged gutters'),
        answer('subtype', 'gutter_installation', 'gutters'),
        answer('work_type', 'replace', 'replaced'),
        answer('intended_result', '6-inch, same color', '6-inch, same color'),
    ], subtype='gutter_installation')
    assert engine.missing_fields(gutter, rubrics.current_snapshot()) == [
        'gutter_downspouts', 'gutter_discharge', 'gutter_guards',
        'gutter_drainage_history', 'gutter_structures', 'gutter_visible_fascia',
    ]
    roof = item('roofing_siding', [
        answer('target_scope', 'whole roof', 'whole roof'),
        answer('desired_outcome', 'replace roof', 'replace roof'),
        answer('subtype', 'roofing', 'roof'),
        answer('work_type', 'replace', 'replace'),
        answer('intended_result', 'similar shingles', 'similar shingles'),
    ], subtype='roofing')
    assert engine.missing_fields(roof, rubrics.current_snapshot()) == [
        'roof_coverage', 'roof_material_direction', 'roof_tearoff',
        'roof_components', 'roof_known_issues',
    ]


def test_siding_replacement_cannot_request_zip_before_trade_scope_is_confirmed():
    siding = item('roofing_siding', [
        answer('target_scope', 'house siding', 'siding of my house'),
        answer('desired_outcome', 'replace vinyl with fiber cement', 'upgrading to hardy board'),
        answer('subtype', 'siding', 'siding'),
        answer('work_type', 'replace', 'replaced'),
        answer('intended_result', 'fiber cement', 'hardy board'),
    ], subtype='siding')
    assert engine.missing_fields(siding, rubrics.current_snapshot()) == [
        'siding_coverage', 'siding_trim_scope', 'siding_finish_direction',
        'siding_known_issues', 'siding_removal',
    ]


@pytest.mark.parametrize(('rubric_id', 'expected_fields'), [
    ('interior_remodeling', {'keep_replace_priorities', 'selection_stage', 'occupancy_constraints'}),
    ('windows_doors', {'operation_and_grid_preferences', 'pricing_structure', 'supply_install_scope',
                       'performance_priorities', 'trim_finish_scope', 'known_opening_damage'}),
    ('handyman', {'supply_responsibility', 'completion_details', 'task_specific_details'}),
    ('interior_cleaning', {'cleaning_extras', 'cleaning_condition_factors', 'cleaning_logistics'}),
    ('decking', {'deck_layout_scope', 'deck_material', 'deck_components', 'deck_removal', 'deck_known_issues'}),
    ('gutter_cleaning', {'gutter_guard_access', 'gutter_brightening', 'gutter_structures_and_damage'}),
    ('power_washing', {'service_branch', 'optional_addons', 'cleaning_level',
                       'added_surface_material', 'concrete_finish_request'}),
    ('moving', {'move_access', 'special_items', 'moving_services', 'model_inventory_coverage'}),
    ('junk_removal', {'special_disposal', 'heavy_disassembly', 'removal_preferences'}),
    ('landscaping', {'existing_landscape_scope', 'ongoing_maintenance',
                     'landscape_materials_and_features', 'landscape_site_and_timing'}),
])
def test_remaining_service_rubrics_include_material_quote_decisions(rubric_id, expected_fields):
    actual = {field.id for field in rubrics.current_snapshot().service(rubric_id).decisions}
    assert expected_fields <= actual


def test_house_wash_asks_addons_and_cleaning_level_but_not_concrete_finish():
    house = item('power_washing', [
        answer('desired_outcome', 'wash house', 'house power washed'),
        answer('target_scope', 'whole house exterior', 'house'),
        answer('service_branch', 'soft_wash', 'house'),
        answer('intended_surfaces', 'whole house exterior', 'house'),
    ])
    assert engine.missing_fields(house, rubrics.current_snapshot()) == ['optional_addons', 'cleaning_level']
    flatwork = item('power_washing', [
        answer('desired_outcome', 'wash driveway', 'driveway washed'),
        answer('target_scope', 'driveway', 'driveway'),
        answer('service_branch', 'flatwork', 'driveway'),
        answer('intended_surfaces', 'driveway', 'driveway'),
        answer('cleaning_level', 'standard_cleaning', 'washed'),
        answer('added_surface_material', 'concrete', 'driveway'),
    ])
    assert engine.missing_fields(flatwork, rubrics.current_snapshot()) == []


def test_incomplete_painting_cannot_be_made_ready_by_model():
    state = state_for('Paint my living room')
    data = result([item(answers=[answer('desired_outcome','paint','Paint'), answer('target_scope','living room','living room')])], zipUpdates=[])
    state = engine.apply_result(state, data)
    assert not state['ready']
    assert state['result']['nextQuestion'] == [dict(workItemId='work', field='included_surfaces')]
    assert 'Walls only' in state['result']['displayText']


def test_unclear_outcome_uses_generic_without_choosing_handyman():
    state = state_for('Fix this room')
    data = result([item('generic_assessment', [answer('target_scope','this room','this room')])],
        serviceCandidates=[], intentEvidence='Fix', zipUpdates=[], action='ask',
        nextQuestion=[dict(workItemId='work', field='desired_outcome')], displayText='What would you like changed or taken care of?')
    applied = engine.apply_result(state, data)
    assert applied['workItems'][0]['service'] is None
    assert not applied['ready']


@pytest.mark.asyncio
@pytest.mark.parametrize('repair_succeeds',[True,False])
async def test_live_validation_repair_keeps_candidate_and_is_bounded(monkeypatch,repair_succeeds):
    state=state_for(); original=copy.deepcopy(state); calls=[]
    invalid=result()
    invalid.workItems[0].answers[-1].value='recommend a finish'
    async def generate(system,payload):
        data=json.loads(payload); calls.append(data)
        if len(calls)==2:
            feedback=data['validationFeedback']
            assert feedback['previousInvalidResponse']==invalid.model_dump()
            assert 'work.finish_preference' in feedback['error']
            if repair_succeeds: return result()
        return invalid
    monkeypatch.setattr(provider,'generate',generate)
    if repair_succeeds:
        assert (await conversation.generate_turn(state,'America/New_York'))['ready']
    else:
        with pytest.raises(ValueError,match='Deferred answers'):
            await conversation.generate_turn(state,'America/New_York')
    assert len(calls)==2
    assert state==original


@pytest.mark.asyncio
async def test_claimed_completion_with_omitted_common_answers_uses_application_question(monkeypatch):
    state=state_for(); calls=[]
    omitted=result()
    omitted.workItems[0].answers=omitted.workItems[0].answers[2:]
    async def generate(system,payload):
        calls.append(json.loads(payload))
        return omitted
    monkeypatch.setattr(provider,'generate',generate)
    applied=await conversation.generate_turn(state,'America/New_York')
    assert not applied['ready'] and applied['result']['nextQuestion']==[dict(workItemId='work',field='desired_outcome')]
    assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('already_answered',[True,False])
async def test_question_review_removes_repetition_but_preserves_real_clarifiers(monkeypatch,already_answered):
    state=state_for() if already_answered else state_for('Paint my living room')
    partial=result([item(answers=[answer('desired_outcome','paint','Paint'),
        answer('target_scope','living room','living room')])],zipUpdates=[],action='ask',
        nextQuestion=[dict(workItemId='work',field='included_surfaces')],
        displayText='Walls only, or should trim and ceilings be included?')
    calls=[]; attempts=[]
    async def generate(system,payload):
        data=json.loads(payload); calls.append(data)
        if len(calls)==1: return partial
        assert data['questionReview']['candidateResponse']==partial.model_dump()
        return result() if already_answered else partial
    monkeypatch.setattr(provider,'generate',generate)
    applied=await conversation.generate_turn(state,'America/New_York',attempts=attempts)
    assert applied['ready']==already_answered
    assert bool(applied['result']['nextQuestion'])!=already_answered
    assert len(calls)==2 and attempts[0]['review']=='proposed_question'


@pytest.mark.asyncio
async def test_invalid_citation_from_question_review_gets_one_final_bounded_repair(monkeypatch):
    state = state_for()
    partial = result([item(answers=[answer('desired_outcome', 'paint', 'Paint'),
        answer('target_scope', 'living room', 'living room')])], zipUpdates=[], action='ask',
        nextQuestion=[dict(workItemId='work', field='included_surfaces')],
        displayText='Walls only, or should trim and ceilings be included?')
    invalid_review = result()
    invalid_review.workItems[0].answers[0].evidence = 'paraphrased homeowner request'
    calls = []
    async def generate(system, payload):
        data = json.loads(payload)
        calls.append(data)
        if len(calls) == 1:
            return partial
        if len(calls) == 2:
            assert 'questionReview' in data
            return invalid_review
        assert data['validationFeedback']['previousInvalidResponse'] == invalid_review.model_dump()
        assert 'exact homeowner text span' in data['validationFeedback']['error']
        return result()
    monkeypatch.setattr(provider, 'generate', generate)
    applied = await conversation.generate_turn(state, 'America/New_York')
    assert applied['ready']
    assert len(calls) == 3


@pytest.mark.parametrize('raw,expected', [('01234','01234'), ('01234-6789','01234-6789'),
    ('012346789','01234-6789'), ('zero one two three four','01234'),
    ('three seven one two nine','37129'), ('0 1 2 3 4 6 7 8 9','01234-6789'),
    ('oh one two three four','01234'), ('1234',None), ('Boston',None), ('twelve thirty four',None),
    ('01234 or 02139',None), ('01234-678',None)])
def test_zip_normalization(raw, expected):
    assert normalize_zip(raw) == expected


def test_moving_destination_does_not_fill_project_zip():
    text='Move everything here to 02139 next Friday; I’ll pack.'
    answers=[answer('desired_outcome','move','Move'), answer('target_scope','everything here','everything here'),
        answer('move_scope','everything here','everything here'), answer('destination_area','02139','02139'),
        answer('timing','next Friday','next Friday'), answer('help_needed','moving only','I’ll pack'),
        answer('move_access','none known','everything here'),
        answer('special_items',None,'everything here','not_applicable'),
        answer('moving_services',None,'I’ll pack','not_applicable')]
    state=engine.apply_result(state_for(text), result([item('moving',answers)], serviceCandidates=['Moving'],
        intentEvidence='Move', zipUpdates=[dict(role='destination', raw='02139', sourceTurnId='turn-1')]))
    assert state['destinationZip']=='02139' and state['projectZip'] is None
    assert state['result']['action']=='ask'
    assert state['result']['nextQuestion'][0]['field']=='model_inventory_coverage'
    assert 'next Friday' in str(state['workItems'])


def test_repeated_zip_request_acknowledges_late_project_detail():
    text='Paint the living room walls only, full repaint, you choose the finish.'
    state=engine.apply_result(state_for(text), result(zipUpdates=[]))
    assert state['result']['action']=='request_zip'
    assert not state['result']['displayText'].startswith('Last thing')
    state['transcript'].append(dict(id='turn-2',role='assistant',text=state['result']['displayText']))
    state['transcript'].append(dict(id='turn-3',role='homeowner',text='Please use low-odor paint too.'))
    state['pendingTurn']={'clientTurnId':'turn-3'}
    followup=result(zipUpdates=[],intentSourceTurnId='turn-3',intentEvidence='low-odor paint')
    updated=engine.apply_result(state,followup)
    assert updated['result']['action']=='request_zip'
    assert updated['result']['displayText'].startswith('Thanks—I’ve added that project detail.')


def test_consecutive_stock_acknowledgement_is_removed():
    state=state_for()
    state['transcript'].append(dict(id='prior-reply',role='assistant',text='Good to know — what finish do you prefer?'))
    proposed=result(zipUpdates=[],action='ask',displayText='Good — should trim be included?',
        nextQuestion=[dict(workItemId='work',field='included_surfaces')])
    proposed.workItems[0].answers=[a for a in proposed.workItems[0].answers if a.field!='included_surfaces']
    applied=engine.apply_result(state,proposed)
    assert applied['result']['displayText']=='Should trim be included?'


def test_landscape_design_is_consultation_first_but_maintenance_is_not():
    snapshot=rubrics.current_snapshot()
    service=snapshot.service('landscaping')
    design_answers=[
        answer('desired_outcome','redesign beds','redesign'), answer('target_scope','front beds','front beds'),
        answer('work_category','landscape_design','redesign'), answer('requested_work','new plants','new plants'),
        answer('target_areas','front beds','front beds'),
        answer('design_preferences',None,'provider can recommend','provider_recommendation'),
        answer('existing_landscape_scope','remove shrubs','remove shrubs'),
        answer('ongoing_maintenance','low maintenance','low maintenance'),
        answer('landscape_materials_and_features',None,'provider can recommend','provider_recommendation'),
        answer('landscape_site_and_timing',None,'not sure','unknown'),
    ]
    design_text=('Please redesign my front beds with new plants, remove shrubs, and make it low maintenance. '
                 'The provider can recommend materials and plants; I am not sure about site conditions. ZIP 01234.')
    state=engine.apply_result(state_for(design_text),
        result([item('landscaping',design_answers)],serviceCandidates=['Landscaping'],intentEvidence='redesign',action='ready'))
    assert not state['ready']
    assert state['result']['action']=='consultation_required'
    maintenance_answers=[
        answer('desired_outcome','mow lawn','Mow'), answer('target_scope','whole lawn','lawn'),
        answer('work_category','maintenance','Mow'), answer('requested_work','mow lawn','Mow'),
        answer('target_areas','whole lawn','lawn'),
    ]
    maintained=engine.apply_result(state_for('Mow my whole lawn. ZIP 01234.'),
        result([item('landscaping',maintenance_answers)],serviceCandidates=['Landscaping'],intentEvidence='Mow'))
    assert maintained['ready']
    assert not service.consultation_before_quote


def test_conditional_fields_and_unknowns_are_not_invented_defaults():
    text='Repair the bedroom floor; please recommend the method.'
    answers=[answer('desired_outcome','repair floor','Repair'),answer('target_scope','bedroom','bedroom'),
             answer('work_type','repair','Repair')]
    state=engine.apply_result(state_for(text),result([item('flooring',answers)],serviceCandidates=['Flooring'],
        intentEvidence='Repair',zipUpdates=[]))
    assert state['result']['action']=='request_zip'
    assert len(state['workItems'][0]['answers'])==3
    answers.append(answer('removal_included','yes','Repair'))
    with pytest.raises(ValueError, match='inapplicable'):
        engine.validate_result(state_for(text),result([item('flooring',answers)],intentEvidence='Repair',zipUpdates=[]))


def test_unknown_finish_satisfies_without_recommendation_and_correction_persists():
    state=engine.apply_result(state_for(),result())
    text='Actually include the ceiling too. I don’t know what finish.'
    state['transcript'].append(dict(id='turn-2',role='homeowner',text=text))
    state['pendingTurn']={'clientTurnId':'turn-2'}
    answers=painting_answers()
    answers[2]=answer('included_surfaces','walls and ceiling','include the ceiling too',source='turn-2')
    answers[4]=answer('finish_preference',None,'I don’t know what finish','unknown',source='turn-2')
    applied=engine.apply_result(state,result([item(answers=answers)],zipUpdates=[],intentSourceTurnId='turn-2',intentEvidence='Actually'))
    assert applied['ready'] and applied['projectZip']=='01234'
    assert applied['workItems'][0]['answers'][4]['status']=='unknown'
    assert not applied['result']['nextQuestion']
    answers.pop(0)
    with pytest.raises(ValueError,match='drop earlier answers'):
        engine.validate_result(state,result([item(answers=answers)],zipUpdates=[],intentSourceTurnId='turn-2',intentEvidence='Actually'))


def test_uncertain_required_scope_becomes_consultation_ready_after_zip():
    state = state_for('Paint my living room. ZIP 01234.')
    state['asked'] = ['work.included_surfaces']
    state['transcript'].append(dict(id='turn-2', role='homeowner', text="I don't know which surfaces need painting."))
    state['pendingTurn'] = {'clientTurnId': 'turn-2'}
    answers = [answer('desired_outcome', 'paint living room', 'Paint my living room'),
        answer('target_scope', 'living room', 'living room'),
        answer('included_surfaces', None, "I don't know which surfaces", 'unknown', source='turn-2')]
    # The application derives assessment from the exact uncertainty evidence
    # even if the model omits that bookkeeping flag.
    proposed = result([item(answers=answers, assessment=False)], action='needs_provider_review',
        zipUpdates=[dict(role='project', raw='01234', sourceTurnId='turn-1')],
        intentSourceTurnId='turn-2', intentEvidence="I don't know")
    applied = engine.apply_result(state, proposed)
    assert not applied['ready']
    assert applied['consultationReady']
    assert applied['result']['action'] == 'consultation_required'
    assert applied['result']['nextQuestion'] == []
    assert 'don’t have to decide that alone' in applied['result']['displayText'].lower()
    assert 'quote consultation' in applied['result']['displayText'].lower()
    package=engine.provider_package(applied)
    assert package['details']['handoffType']=='consultation_request'
    Handoff.model_validate(package['details'])


def test_provider_handoff_includes_private_supplemental_photo_references():
    state=engine.apply_result(state_for(),result())
    state['supplementalPhotos']=[dict(id='photo-1',fileName='Safe attic view.jpg',mimeType='image/jpeg',
        byteCount=1234,storageBucket='metashape-exports',storagePath='owner/home/photo.jpg',
        thumbnailPath='owner/home/photo-thumb.jpg')]
    package=engine.provider_package(state)
    handoff=Handoff.model_validate(package['details'])
    assert handoff.supplementalPhotos[0].fileName=='Safe attic view.jpg'
    assert '1 attached' in package['description']


def test_quote_intake_photo_upload_schema_is_bounded():
    payload=dict(conversationId=str(uuid4()),homeId=str(uuid4()),clientTurnId=str(uuid4()),expectedRevision=0,
        text='Please include this safe attic photo.',attachments=[dict(id=str(uuid4()),fileName='attic.jpg',
            mimeType='image/jpeg',dataBase64='YWJj')])
    assert len(TurnRequest.model_validate(payload).attachments)==1
    payload['attachments'][0]['mimeType']='application/pdf'
    with pytest.raises(ValueError): TurnRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_supplemental_photo_is_normalized_and_privately_stored(monkeypatch):
    output=io.BytesIO();Image.new('RGB',(32,24),'blue').save(output,format='PNG')
    upload=SimpleNamespace(id=uuid4(),fileName='attic.png',mimeType='image/png',
        dataBase64=base64.b64encode(output.getvalue()).decode())
    writes=[]
    async def save(bucket,path,data,content_type):
        writes.append((bucket,path,len(data),content_type));return 'https://signed.example/photo'
    monkeypatch.setattr(api.supabase_store,'upload_bytes',save)
    refs=await api.store_supplemental_photos([upload],str(uuid4()),str(uuid4()),str(uuid4()))
    assert len(writes)==2 and all(write[3]=='image/jpeg' for write in writes)
    assert refs[0]['mimeType']=='image/jpeg' and refs[0]['thumbnailPath'].endswith('-thumb.jpg')


def test_required_uncertainty_uses_provider_review_for_no_consultation_service():
    state = state_for("I need my windows cleaned. ZIP 01234.")
    state['asked'] = ['work.sides_included']
    state['transcript'].append(dict(id='turn-2', role='homeowner', text="I'm not sure whether inside or outside."))
    state['pendingTurn'] = {'clientTurnId': 'turn-2'}
    answers = [answer('desired_outcome', 'clean windows', 'windows cleaned'),
        answer('target_scope', 'my windows', 'my windows'),
        answer('sides_included', None, "I'm not sure whether inside or outside", 'unknown', source='turn-2'),
        answer('extras', None, "I'm not sure", 'unknown', source='turn-2'),
        answer('window_type_details', None, "I'm not sure", 'unknown', source='turn-2'),
        answer('cleaning_condition', None, "I'm not sure", 'unknown', source='turn-2')]
    proposed = result([item('window_cleaning', answers=answers, assessment=False)],
        serviceCandidates=['Window Cleaning'], action='needs_provider_review',
        intentSourceTurnId='turn-2', intentEvidence="I'm not sure")
    applied = engine.apply_result(state, proposed)
    assert applied['ready']
    assert applied['result']['action'] == 'needs_provider_review'


def test_window_cleaning_questions_focus_on_price_inputs_not_story_count():
    service = rubrics.current_snapshot().service('window_cleaning')
    fields = {decision.id: decision for decision in service.decisions}
    assert {'window_type_details', 'cleaning_condition', 'interior_access'} <= fields.keys()
    assert fields['interior_access'].when.field == 'sides_included'
    assert fields['interior_access'].when.one_of == ['inside', 'both']
    questions = ' '.join(decision.question.lower() for decision in service.decisions)
    assert 'storm windows' in questions and 'exterior-grid' in questions
    assert 'hard-water' in questions and 'construction debris' in questions
    assert 'interior glass' in questions and 'vaulted room' in questions
    assert 'stories' not in questions and 'story count' not in questions


def test_flooring_rubric_separates_lifestyle_model_and_technical_inputs():
    service = rubrics.current_snapshot().service('flooring')
    fields = {decision.id: decision for decision in service.decisions}
    assert {'detail_scope', 'lifestyle_priorities', 'moving_responsibility'} <= fields.keys()
    assert 'pets' in fields['lifestyle_priorities'].question.lower()
    assert 'children' in fields['lifestyle_priorities'].question.lower()
    assert 'maintenance' in fields['lifestyle_priorities'].question.lower()
    model_review = ' '.join(service.provider_model_review).lower()
    technical_review = ' '.join(service.provider_technical_review).lower()
    assert 'room-by-room takeoff' in model_review and 'thresholds' in model_review
    assert 'subfloor' in technical_review and 'moisture' in technical_review
    assert 'longer-lasting' not in ' '.join(service.quote_options).lower()


def test_roof_leak_is_assessment_with_provider_diagnosis():
    text='The roof leaks above this room. Please assess the leak.'
    answers=[answer('desired_outcome','assess leak','assess the leak'),answer('target_scope','roof leak above this room','roof leaks above this room'),
        answer('subtype','roofing','roof'),answer('work_type','assess','assess')]
    applied=engine.apply_result(state_for(text),result([item('roofing_siding',answers,subtype='roofing',assessment=True)],
        serviceCandidates=['Roofing & Siding'],intentEvidence='Please assess',zipUpdates=[]))
    applied['projectZip']='01234'
    package=engine.provider_package(applied)
    assert 'Never infer exterior coverage' in package['description']
    assert 'cause of a leak' in package['description']
    assert 'provider assessment' in package['description']


def test_provider_brief_quotes_freeform_intent_without_expanding_assessment():
    text='The roof leaks above this room. Please assess the leak and recommend repairs.'
    answers=[answer('desired_outcome','Assess and repair the roof','Please assess the leak and recommend repairs'),
        answer('target_scope','Roof area above this room','The roof leaks above this room'),
        answer('subtype','roofing','roof'),answer('work_type','assess','assess')]
    state=engine.apply_result(state_for(text),result([item('roofing_siding',answers,subtype='roofing',assessment=True)],
        serviceCandidates=['Roofing & Siding'],intentEvidence='Please assess',zipUpdates=[]))
    state['projectZip']='01234'
    description=engine.provider_package(state)['description']
    assert 'Desired outcome: Please assess the leak and recommend repairs' in description
    assert 'Assess and repair the roof' not in description
    assert 'Work type: assess' in description


def test_multiservice_and_unsupported_remain_separate():
    text=PAINT+' Diagnose a leaking sink in the kitchen.'
    plumbing=item('generic_assessment',[answer('desired_outcome','diagnose leak','Diagnose a leaking sink'),
        answer('target_scope','kitchen sink','sink in the kitchen'),answer('desired_help','diagnose leak','Diagnose a leaking sink')],
        assessment=True,id='plumbing')
    applied=engine.apply_result(state_for(text),result([item(answers=painting_answers()),plumbing]))
    assert applied['ready'] and len(applied['workItems'])==2
    assert applied['workItems'][1]['service'] is None
    assert applied['result']['action']=='needs_provider_review'


@pytest.mark.parametrize('mutation', ['bad_provenance','unknown_field','bad_catalog','default_subtype','repeat_question','visual_claim','price','geometry'])
def test_untrusted_model_cannot_bypass_validation(mutation):
    data=result().model_dump()
    if mutation=='bad_provenance': data['workItems'][0]['answers'][0]['sourceTurnId']='another-home'
    if mutation=='unknown_field': data['workItems'][0]['answers'][0]['field']='wall_area'
    if mutation=='bad_catalog': data['workItems'][0]['service']='Plumbing'
    if mutation=='default_subtype': data['workItems'][0]['subtype']='roofing'
    if mutation=='repeat_question': data['nextQuestion']=[dict(workItemId='work',field='finish_preference')]
    if mutation=='visual_claim': data['displayText']='Your scan shows paint damage.'
    if mutation=='price': data['displayText']='This costs 500 dollars.'
    if mutation=='geometry': data['displayText']='What is the ceiling height?'
    with pytest.raises(ValueError): engine.validate_result(state_for(),Result.model_validate(data))


def test_allowlist_does_not_spread_scan_or_legacy_context():
    state=state_for()
    state.update(homeContext={'geometry':'SECRET_GEOMETRY'}, scanContext={'url':'SECRET_URL'},
        oldConversation=['SECRET_HISTORY'], secrets='SECRET_KEY')
    payload=engine.model_input(state,'America/New_York')
    assert 'SECRET_' not in payload
    decoded=json.loads(payload)
    assert set(decoded)=={'transcript','homeownerAnswers','sharedAnswers','supportedCatalogLabels','serviceRubrics','answerFieldIdsByRubric','workflow'}
    common={f['id'] for f in state['rubric']['common']['required']}
    for service in state['rubric']['services']:
        assert set(decoded['answerFieldIdsByRubric'][service['id']])==common|{f['id'] for f in service['decisions']}
    assert json.loads(payload)['workflow']['scanVisibleToAgent'] is False


def test_rubric_edit_changes_new_snapshot_not_existing(monkeypatch):
    old=engine.initial_state()
    edited=rubrics.RUBRIC_PATH.read_text().replace('Walls only, or should the quote include trim, ceilings, or other surfaces?', 'Which surfaces should the painter include?')
    snapshot=rubrics.parse_document(edited)
    monkeypatch.setattr(engine,'current_snapshot',lambda:snapshot)
    new=engine.initial_state()
    assert new['rubric']['content_hash']!=old['rubric']['content_hash']
    assert 'Which surfaces' in new['rubric']['services'][0]['decisions'][0]['question']
    assert 'Walls only' in old['rubric']['services'][0]['decisions'][0]['question']


@pytest.mark.parametrize('before,after', [('id: painting','id: flooring'),('catalog_service: Painting','catalog_service: Plumbing'),
    ('when: {field: work_type, equals: replace}','when: {field: nonexistent, equals: replace}'),
    ('when: {field: work_type, equals: replace}','when: "exec(evil)"'),
    ('priority: 1','priority: 0'),('defer: []','defer: []\n    defer: [unknown]'),
    ('equals: replace','equals: invalid'),('id: included_surfaces','id: extent')])
def test_invalid_rubric_edits_fail_loudly(before,after):
    with pytest.raises(ValueError): rubrics.parse_document(rubrics.RUBRIC_PATH.read_text().replace(before,after,1))


@pytest.mark.asyncio
async def test_api_one_turn_retry_and_exactly_one_submission(direct,monkeypatch):
    client, direct_body, upload, scheduled=direct
    calls=[]
    async def generate(system,payload): calls.append(json.loads(payload)); return mapped_reply(payload)
    monkeypatch.setattr(provider,'generate',generate)
    body=wire(direct_body['homeId'])
    first=await client.post('/api/v1/ai/quote-intake/turn',json=body)
    assert first.status_code==200,first.text
    assert first.json()['ready']
    again=await client.post('/api/v1/ai/quote-intake/turn',json=body)
    assert again.json()==first.json() and len(calls)==1
    changed=dict(body,text=PAINT+' extra')
    assert (await client.post('/api/v1/ai/quote-intake/turn',json=changed)).status_code==409
    submission={k:body[k] for k in ('conversationId','homeId')}
    submission.update(expectedRevision=1,uploadRevision=direct_body['revision'])
    receipt=await client.post('/api/v1/ai/quote-intake/submit',json=submission)
    assert receipt.status_code==200,receipt.text
    retry=await client.post('/api/v1/ai/quote-intake/submit',json=submission)
    assert retry.json()['receipt']==receipt.json()['receipt']
    assert len(scheduled)==1 and len(await quote_store.list())==1
    record=await quote_store.get(receipt.json()['receipt']['quoteRequestId'])
    assert record.scopeIntent=='described_scope' and record.serviceType=='Painting'
    assert record.quoteDraft['intake']['workItems'][0]['answers'][-1]['status']=='provider_recommendation'
    assert 'provider recommendation requested' in record.synopsis and record.scanMedia
    assert not any('homeContext' in p for p in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff_type,label", [("quote_request","quote request"),("consultation_request","consultation request")])
async def test_quote_and_consultation_requests_queue_and_deliver_support_email(direct, monkeypatch, handoff_type, label):
    client, direct_body, _, scheduled = direct
    monkeypatch.setattr(settings, 'ops_email', 'support@takeshapehome.com')
    deliveries = []

    async def send(subject, body, html=None, **kwargs):
        deliveries.append(dict(subject=subject, body=body, html=html, **kwargs))
        return 'sent'

    monkeypatch.setattr(ops_email, 'send_ops_message', send)
    state = engine.apply_result(state_for(), result())
    handoff = engine.provider_package(state)['details']
    handoff['handoffType'] = handoff_type
    body = dict(direct_body, submissionId=str(uuid4()), intakeDetails=handoff)
    response = await client.post('/api/v1/ai/quote-requests/direct', json=body)
    assert response.status_code == 201, response.text
    record = await quote_store.get(response.json()['quoteRequestId'])
    assert record.quoteDraft['intake']['handoffType'] == handoff_type
    subject, text = ops_email.build_ops_email(record, [], [])
    html = ops_email.build_ops_email_html(record, [], [])
    assert subject.startswith(f'New {label}:')
    if handoff_type == 'consultation_request':
        assert 'Request type: Consultation Request' in text
        assert 'Consultation request' in html
    else:
        assert 'Request type:' not in text
    record.opsEmailPayload = dict(subject=subject, body=text, html=html,
        to=settings.ops_email, cc=['noah@takeshapehome.com'])
    await quote_store.save(record, require_durable=True)
    assert await ops_email.send_ops_email(record) == 'sent'
    delivered = await quote_store.get(record.id)
    assert delivered.opsEmailDeliveredAt
    assert await ops_email.send_ops_email(delivered) == 'already_sent'


    assert len(scheduled) == 1
    assert [delivery['to'] for delivery in deliveries] == [settings.ops_email]
    assert all(delivery['cc'] == ['noah@takeshapehome.com'] for delivery in deliveries)


@pytest.mark.asyncio
async def test_upload_wait_pause_edit_and_revision_gate(direct,monkeypatch):
    client, original, upload, scheduled=direct
    async def generate(system,payload): return mapped_reply(payload)
    monkeypatch.setattr(provider,'generate',generate)
    body=wire(original['homeId'])
    await client.post('/api/v1/ai/quote-intake/turn',json=body)
    submit=dict(conversationId=body['conversationId'],homeId=body['homeId'],expectedRevision=1,uploadRevision=str(uuid4()))
    response=await client.post('/api/v1/ai/quote-intake/submit',json=submit)
    assert response.status_code==409 and not scheduled
    async def pause(system,payload):
        data=result().model_dump()
        data.update(intent='pause',intentEvidence='Pause',intentSourceTurnId=json.loads(payload)['transcript'][-1]['id'],zipUpdates=[])
        for a in data['workItems'][0]['answers']: a['sourceTurnId']=body['clientTurnId']
        return Result.model_validate(data)
    monkeypatch.setattr(provider,'generate',pause)
    paused=await client.post('/api/v1/ai/quote-intake/turn',json=wire(body['homeId'],'Pause this request.',1,body['conversationId']))
    assert paused.status_code==200,paused.text
    assert paused.json()['paused'] and not paused.json()['ready']
    submit['uploadRevision']=original['revision']
    assert (await client.post('/api/v1/ai/quote-intake/submit',json=submit)).status_code==409
    assert not scheduled


@pytest.mark.asyncio
async def test_malformed_output_retry_and_lost_success_recovery(direct,monkeypatch):
    client, original, upload, scheduled=direct
    calls=[]
    async def fail(system,payload): calls.append(1); raise ValueError('malformed')
    monkeypatch.setattr(provider,'generate',fail)
    body=wire(original['homeId'])
    response=await client.post('/api/v1/ai/quote-intake/turn',json=body)
    assert response.status_code==503 and len(calls)==2
    saved=await client.get(f"/api/v1/ai/quote-intake/{body['conversationId']}?homeId={body['homeId']}")
    assert saved.json()['messages'][0]['text']==PAINT and saved.json()['pendingTurnId']==body['clientTurnId']
    async def recover(system,payload): return mapped_reply(payload)
    monkeypatch.setattr(provider,'generate',recover)
    assert (await client.post('/api/v1/ai/quote-intake/turn',json=body)).json()['ready']
    direct_submit=api.submit_direct_quote_request
    async def lose_success(*args):
        await direct_submit(*args)
        raise HTTPException(503,'Lost response after durable receipt')
    monkeypatch.setattr(api,'submit_direct_quote_request',lose_success)
    submit=dict(conversationId=body['conversationId'],homeId=body['homeId'],expectedRevision=1,uploadRevision=original['revision'])
    assert (await client.post('/api/v1/ai/quote-intake/submit',json=submit)).status_code==503
    monkeypatch.setattr(api,'submit_direct_quote_request',direct_submit)
    upload['revision']=str(uuid4())
    recovered=await client.post('/api/v1/ai/quote-intake/submit',json=submit)
    assert recovered.status_code==200,recovered.text
    assert recovered.json()['receipt'] and len(await quote_store.list())==1


@pytest.mark.asyncio
async def test_double_send_and_owner_scan_isolation(direct,monkeypatch):
    client, original, _, _=direct
    gate=asyncio.Event(); started=asyncio.Event(); calls=[]
    async def generate(system,payload):
        calls.append(1); started.set(); await gate.wait(); return mapped_reply(payload)
    monkeypatch.setattr(provider,'generate',generate)
    body=wire(original['homeId'])
    task=asyncio.create_task(client.post('/api/v1/ai/quote-intake/turn',json=body))
    await started.wait()
    assert (await client.post('/api/v1/ai/quote-intake/turn',json=body)).status_code==409
    gate.set(); assert (await task).status_code==200 and len(calls)==1
    async def another_owner(home,token): return str(uuid4())
    monkeypatch.setattr(api,'_scan_upload_owner',another_owner)
    assert (await client.post('/api/v1/ai/quote-intake/turn',json=body)).status_code==404
    assert (await client.get(f"/api/v1/ai/quote-intake/{body['conversationId']}?homeId={body['homeId']}")).status_code==404


@pytest.mark.asyncio
async def test_cas_fences_late_worker_and_local_state_survives_reload(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'storage_dir',str(tmp_path))
    id,owner,home=map(str,(uuid4(),uuid4(),uuid4()))
    initial=engine.initial_state()
    v=await store.save(id,owner,home,initial,None)
    next_state=dict(initial,revision=1)
    await store.save(id,owner,home,next_state,v)
    with pytest.raises(HTTPException) as exc: await store.save(id,owner,home,initial,v)
    assert exc.value.status_code==409
    assert (await store.read(id,owner,home))['state']['revision']==1
    with pytest.raises(HTTPException): await store.read(id,owner,str(uuid4()))


@pytest.mark.asyncio
async def test_transport_uses_full_new_schema_and_text_only(monkeypatch):
    monkeypatch.setattr(settings,'anthropic_api_key','fixture-key')
    captured={}
    class Client:
        def __init__(self,**kwargs): self.messages=self
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def parse(self,**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(stop_reason='end_turn',parsed_output=result(),model='fixture',_request_id='fixture',
                usage=SimpleNamespace(input_tokens=1,output_tokens=1))
    monkeypatch.setattr(provider.anthropic,'AsyncAnthropic',Client)
    await provider.generate('system','{}')
    assert captured['output_format'] is Result
    assert captured['messages']==[{'role':'user','content':[{'type':'text','text':'{}'}]}]
    assert 'tools' not in captured and 'compact_schema' not in captured


@pytest.mark.asyncio
async def test_supabase_cas_filters_owner_scan_version_and_never_falls_back(monkeypatch,tmp_path):
    import httpx
    from app.flow import supabase_store
    monkeypatch.setattr(settings,'storage_dir',str(tmp_path))
    monkeypatch.setattr(supabase_store,'enabled',lambda:True)
    seen=[]
    def handler(request):
        seen.append(request)
        if request.method=='PATCH':
            assert request.url.params['owner_id']=='eq.owner'
            assert request.url.params['home_id']=='eq.home'
            assert request.url.params['version']=='eq.4'
            assert json.loads(request.content)['version']==5
            return httpx.Response(200,json=[])
        return httpx.Response(503)
    async with httpx.AsyncClient(base_url='https://example.test/rest/v1',transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(supabase_store,'_rest',lambda:client)
        with pytest.raises(HTTPException) as exc:
            await store.save('id','owner','home',{},4)
        assert exc.value.status_code==409
        with pytest.raises(HTTPException) as exc:
            await store.read('id','owner','home')
        assert exc.value.status_code==503
    assert not (tmp_path/'quote_intake.sqlite3').exists()


def test_early_zip_reused_and_ambiguity_gets_one_clarifier():
    state=state_for(PAINT.replace('01234','twelve thirty four'))
    data=result(zipUpdates=[dict(role='project',raw='twelve thirty four',sourceTurnId='turn-1')])
    first=engine.apply_result(state,data)
    assert first['zipClarifications']==1 and first['projectZip'] is None
    second=engine.apply_result(first,data)
    assert second['zipClarifications']==1 and 'whenever' in second['result']['displayText']
    state=state_for()
    state['projectZip']='01234'
    applied=engine.apply_result(state,result(zipUpdates=[]))
    assert applied['ready'] and applied['result']['action']=='needs_provider_review'


def test_unsupported_assessment_accepts_persistent_uncertainty_without_designer_call():
    state=state_for('Not sure what to change; please assess this room.')
    state['asked']=['work.desired_outcome']
    answers=[answer('target_scope','this room','this room'),
        answer('desired_outcome',None,'Not sure what to change','provider_review'),
        answer('desired_help',None,'please assess this room','provider_review')]
    data=result([item('generic_assessment',answers,assessment=True)],serviceCandidates=[],intentEvidence='please assess',zipUpdates=[])
    applied=engine.apply_result(state,data)
    assert applied['result']['action']=='request_zip' and not applied['result']['nextQuestion']


@pytest.mark.asyncio
async def test_turn_requires_authenticated_owner_and_rejects_visual_fields(direct):
    client,original,_,_=direct
    body=wire(original['homeId'])
    assert (await client.post('/api/v1/ai/quote-intake/turn',json=body,headers={'X-Homeowner-Token':'wrong'})).status_code==401
    for key in ('homeContext','scanContext','ownerId','images','roomKeys'):
        assert (await client.post('/api/v1/ai/quote-intake/turn',json=dict(body,**{key:{}}))).status_code==422


@pytest.mark.asyncio
async def test_old_turn_replay_returns_latest_answers_not_old_snapshot(direct,monkeypatch):
    client,original,_,_=direct
    first_body=wire(original['homeId'])
    async def generate(system,payload):
        data=json.loads(payload)
        if len(data['transcript'])==1: return mapped_reply(payload)
        changed=result().model_dump()
        for a in changed['workItems'][0]['answers']: a['sourceTurnId']=first_body['clientTurnId']
        changed['workItems'][0]['answers'][2]=answer('included_surfaces','walls and ceiling','include the ceiling',source=data['transcript'][-1]['id'])
        changed.update(zipUpdates=[],intentSourceTurnId=data['transcript'][-1]['id'],intentEvidence='include the ceiling')
        return Result.model_validate(changed)
    monkeypatch.setattr(provider,'generate',generate)
    await client.post('/api/v1/ai/quote-intake/turn',json=first_body)
    second_body=wire(original['homeId'],'Also include the ceiling',1,first_body['conversationId'])
    second=await client.post('/api/v1/ai/quote-intake/turn',json=second_body)
    assert second.status_code==200,second.text
    replay=await client.post('/api/v1/ai/quote-intake/turn',json=first_body)
    assert replay.json()['revision']==2 and len(replay.json()['messages'])==4
    assert replay.json()['result']['workItems'][0]['answers'][2]['value']=='walls and ceiling'


def test_zip_turn_preserves_prior_answers_when_model_rephrases_or_drops_them():
    text = PAINT.replace(', ZIP 01234.', '.')
    state = state_for(text)
    state = engine.apply_result(state, result(zipUpdates=[]))
    assert state['result']['action'] == 'request_zip'
    state['transcript'].append(dict(id='turn-2', role='homeowner', text='01234'))
    state['pendingTurn'] = dict(clientTurnId='turn-2')
    drifted = result(zipUpdates=[dict(role='project', raw='01234', sourceTurnId='turn-2')],
        intentSourceTurnId='turn-2', intentEvidence='01234')
    drifted.workItems[0].answers = drifted.workItems[0].answers[1:]
    drifted.workItems[0].answers[0].value = 'a rephrased room scope'
    applied = engine.apply_result(state, drifted)
    restored = {a['field']: a for a in applied['workItems'][0]['answers']}
    assert applied['ready'] and applied['projectZip'] == '01234'
    assert restored['desired_outcome']['value'] == 'full repaint'
    assert restored['target_scope']['value'] == 'living room'


def test_interior_remodeling_collects_zip_then_routes_to_consultation_first():
    text = ('Refresh the kitchen before listing on June 1. Keep the layout. Paint the cabinets, replace the '
            'countertop, backsplash, and sink. Use a subtle modern style. Help select products. The kitchen may '
            'be shut down. Compare cost-conscious and premium options. ZIP 37130.')
    answers = [
        answer('desired_outcome', 'refresh before sale', 'Refresh the kitchen before listing'),
        answer('target_scope', 'kitchen', 'the kitchen'),
        answer('intended_changes', 'cabinet paint, countertop, backsplash, sink',
               'Paint the cabinets, replace the countertop, backsplash, and sink'),
        answer('layout_intent', 'keep layout', 'Keep the layout'),
        answer('design_direction', 'subtle modern', 'subtle modern style'),
        answer('keep_replace_priorities', 'keep cabinets and replace other finishes',
               'Paint the cabinets, replace the countertop, backsplash, and sink'),
        answer('selection_stage', None, 'Help select products', 'provider_recommendation'),
        answer('occupancy_constraints', None, 'The kitchen may be shut down', 'not_applicable'),
        answer('listing_timeline', 'June 1', 'June 1'),
        answer('resale_investment_direction', 'compare_options', 'Compare cost-conscious and premium options'),
    ]
    proposed = result([item('interior_remodeling', answers)],
        serviceCandidates=['Interior Remodeling'], intentEvidence='Refresh',
        zipUpdates=[dict(role='project', raw='37130', sourceTurnId='turn-1')])
    applied = engine.apply_result(state_for(text), proposed)
    assert not applied['ready']
    assert applied['projectZip'] == '37130'
    assert applied['result']['action'] == 'consultation_required'
    assert 'normally starts with a quote consultation' in applied['result']['displayText']
    assert 'Consolidated homeowner scope statements:' in engine.provider_package(applied)['description']


def test_model_cannot_silently_choose_ambiguous_zip_or_use_moving_destination():
    state=state_for(PAINT.replace('01234','01234 or 02139'))
    applied=engine.apply_result(state,result())
    assert applied['projectZip'] is None and not applied['ready']
    state=state_for(PAINT.replace('ZIP 01234','moving to 01234'))
    with pytest.raises(ValueError,match='destination'):
        engine.apply_result(state,result())


@pytest.mark.asyncio
async def test_malformed_direct_handoff_is_rejected_before_dispatch(direct):
    client,body,_,scheduled=direct
    for handoff in ({'workItems':'oops'}, {'workItems':[42]}, {'workItems':None}):
        response=await client.post('/api/v1/ai/quote-requests/direct',json=dict(body,intakeDetails=handoff))
        assert response.status_code==422
    assert not scheduled


@pytest.mark.parametrize('text', [
    "Stop. Don't submit this request.",
    'Cancel.',
    'Please cancel my request.',
])
def test_explicit_stop_blocks_dispatch_even_if_model_misclassifies_intent(text):
    state=engine.apply_result(state_for(),result())
    state['transcript'].append(dict(id='turn-2',role='homeowner',text=text))
    state['pendingTurn']={'clientTurnId':'turn-2'}
    misleading=result(intentSourceTurnId='turn-2',intentEvidence=text,intent='request',zipUpdates=[])
    applied=engine.apply_result(state,misleading)
    assert applied['paused'] and not applied['ready']
    assert 'paused' in applied['result']['displayText']


@pytest.mark.asyncio
async def test_official_sdk_structured_output_wire_preserves_new_fields(monkeypatch):
    import anthropic
    import httpx2
    monkeypatch.setattr(settings,'anthropic_api_key','fixture-key')
    actual_client=anthropic.AsyncAnthropic
    captured=[]
    def transport(request):
        captured.append(json.loads(request.content))
        return httpx2.Response(200,json=dict(id='msg_fixture',type='message',role='assistant',model='fixture',
            content=[dict(type='text',text=result().model_dump_json())],stop_reason='end_turn',stop_sequence=None,
            usage=dict(input_tokens=10,output_tokens=10)))
    monkeypatch.setattr(provider.anthropic,'AsyncAnthropic',lambda **kwargs: actual_client(**kwargs,
        base_url='https://fixture.invalid',http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(transport))))
    output=await provider.generate('test system','{"synthetic":true}')
    assert output.workItems[0].answers[-1].status=='provider_recommendation'
    schema=captured[0]['output_config']['format']['schema']
    assert {'workItems','zipUpdates','nextQuestion','removedWorkItems','intentSourceTurnId'} <= schema['properties'].keys()
    assert 'flowCapture' not in schema['properties']
    assert captured[0]['output_config']['effort']==settings.anthropic_effort


def test_full_context_and_answers_survive_more_than_eight_messages():
    state=engine.apply_result(state_for(),result())
    for number in range(2,14):
        state['transcript'].extend([dict(id=f'turn-{number}',role='homeowner',text='Keep that scope.'),
                                   dict(id=f'turn-{number}-reply',role='assistant',text='Scope saved.')])
    payload=json.loads(engine.model_input(state,'America/New_York'))
    assert payload['transcript'][0]['text']==PAINT
    assert len(payload['transcript'])==25
    assert payload['homeownerAnswers'][0]['answers'][-1]['sourceTurnId']=='turn-1'
    assert payload['workflow']['zipAnswers'][0]['sourceTurnId']=='turn-1'
