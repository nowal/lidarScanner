"""Opt-in Claude evaluation using only synthetic fixtures; NEVER submits leads.

Default only validates the versioned cases and rubric. --live uses the configured
Anthropic account and writes a report to the supplied --output path. It imports
no API, quote submission, email, scan, or production persistence module.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.quote_intake import conversation, engine
from app.quote_intake.rubrics import current_snapshot

FIXTURES = Path(__file__).resolve().parents[1] / 'tests/fixtures/quote_intake/conversations_v1.json'


def check_case(case, state, questions):
    failures=[]
    for expected, actual in [('expectedReady', state.get('ready',False)), ('expectedZip',state['projectZip']),
                             ('expectedPaused',state['paused']), ('expectedWorkItems',len(state['workItems']))]:
        if expected in case and case[expected]!=actual: failures.append(expected)
    if 'expectedService' in case and case['expectedService'] not in [i['service'] for i in state['workItems']]:
        failures.append('expectedService')
    if case.get('expectedUnmapped') and not any(i['service'] is None for i in state['workItems']): failures.append('expectedUnmapped')
    if case.get('expectedQuestion') and not any(q['field']==case['expectedQuestion'] for q in (state['result'] or {}).get('nextQuestion',[])):
        failures.append('expectedQuestion')
    if case.get('expectedAction') and case['expectedAction'] != (state.get('result') or {}).get('action'):
        failures.append('expectedAction')
    for field,status in case.get('expectedStatus',{}).items():
        if not any(a['field']==field and a['status']==status for i in state['workItems'] for a in i['answers']): failures.append('expectedStatus.'+field)
    if questions>case['maxClarifications']: failures.append('unnecessaryQuestions')
    return failures


async def evaluate(cases):
    reports=[]
    for case in cases:
        state=engine.initial_state(); latencies=[]; replies=[]; errors=[]; questions=0; attempts=[]
        homeowner_turns = list(case.get('turns') or [case['persona']['opening']])
        turn_index = 0
        while turn_index < len(homeowner_turns):
            text = homeowner_turns[turn_index]
            turn_index += 1
            tid=str(uuid4()); state['transcript'].append(dict(id=tid,role='homeowner',text=text))
            state['pendingTurn']={'clientTurnId':tid}
            start=time.monotonic()
            try:
                turn_attempts=[]; attempts.append(turn_attempts)
                state=await conversation.generate_turn(state,'America/New_York',attempts=turn_attempts)
                reply=state['result']; questions += bool(reply['nextQuestion'])
                replies.append(reply)
                state['transcript'].append(dict(id=tid+'-reply',role='assistant',text=reply['displayText']))
                state['revision']+=1;state['pendingTurn']=None
                if reply['action'] in ('ready', 'needs_provider_review', 'consultation_required'):
                    break
                persona = case.get('persona')
                if persona and turn_index == len(homeowner_turns) and turn_index < persona['maxTurns']:
                    if reply['action'] == 'request_zip':
                        next_text = persona['projectZip']
                    elif reply['nextQuestion']:
                        fields = [q['field'] for q in reply['nextQuestion']]
                        next_text = next((persona['answersByField'].get(field) for field in fields
                                          if persona['answersByField'].get(field)), None)
                        if next_text is None:
                            errors.append('PersonaMissingAnswer: ' + ','.join(fields))
                            break
                    else:
                        errors.append('PersonaCannotContinue: no question or ZIP request')
                        break
                    homeowner_turns.append(next_text)
            except Exception as exc:
                errors.append(type(exc).__name__ + ': ' + str(exc)[:200])
                break
            finally: latencies.append(round(time.monotonic()-start,3))
        repeats=len(state['asked'])-len(set(state['asked']))
        reports.append(dict(id=case['id'],failures=check_case(case,state,questions)+errors,replies=replies,
            homeownerTurns=homeowner_turns[:len(replies)],
            structuredAnswers=state['workItems'],clarificationTurns=questions,repeatedQuestionCount=repeats,modelAttempts=attempts,
            latencySeconds=latencies,replyWordCounts=[len(r['displayText'].split()) for r in replies],
            providerBrief=engine.provider_package(state)['description']
                if state.get('ready') or state.get('consultationReady') else None))
        print(f"{case['id']}: {'FAIL ' + str(reports[-1]['failures']) if reports[-1]['failures'] else 'PASS'} ({sum(latencies):.1f}s)",flush=True)
    return reports


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--output',type=Path)
    parser.add_argument('--fixtures', type=Path, default=FIXTURES)
    parser.add_argument('--case',action='append',dest='case_ids',help='Run a named fixture; repeat to select several cases')
    args=parser.parse_args();fixture=json.loads(args.fixtures.read_text());snapshot=current_snapshot()
    ids=[case['id'] for case in fixture['cases']]
    if len(ids)!=len(set(ids)): parser.error('Duplicate evaluation case IDs')
    if args.case_ids:
        if set(args.case_ids)-set(ids): parser.error('Unknown evaluation case ID')
        fixture['cases']=[case for case in fixture['cases'] if case['id'] in args.case_ids]
    for case in fixture['cases']:
        if ('turns' in case) == ('persona' in case):
            parser.error('Each case needs exactly one of turns or persona')
        if 'turns' in case and (not case['turns'] or any(not isinstance(t,str) or not t.strip() for t in case['turns'])):
            parser.error('Each case needs nonempty homeowner turns')
        if 'persona' in case:
            persona=case['persona']
            if (not isinstance(persona.get('opening'),str) or not persona['opening'].strip()
                    or not isinstance(persona.get('projectZip'),str) or not persona['projectZip'].strip()
                    or not isinstance(persona.get('answersByField'),dict)
                    or not isinstance(persona.get('maxTurns'),int) or persona['maxTurns'] < 2):
                parser.error('Invalid adaptive persona')
        if not isinstance(case['maxClarifications'],int) or case['maxClarifications']<0:
            parser.error('Invalid clarification target')
        if 'expectedService' in case and case['expectedService'] not in engine.KNOWN_SERVICE_TYPES:
            parser.error('Expected service is outside the catalog')
    if not args.live:
        print(f"Validated {fixture['version']}: {len(fixture['cases'])} cases; rubric {snapshot.content_hash}. No model calls or delivery.")
        return
    if not args.output: parser.error('--live requires --output (report contains synthetic fixtures only)')
    if not settings.anthropic_api_key: parser.error('No configured Anthropic account; live evaluation was not run')
    initial=engine.initial_state()
    report=dict(version=fixture['version'],realModel=True,model=settings.anthropic_model,effort=settings.anthropic_effort,
        promptVersion=initial['promptVersion'],promptHash=initial['promptHash'],
        rubricHash=snapshot.content_hash,submissionCalls=0,
        measurementNotes='Question/fact guards are deterministic. Human review of semantics, brief completeness and abandonment risk is still required. This runner never dispatches; duplicate submission is covered by API tests.',
        cases=asyncio.run(evaluate(fixture['cases'])))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False))
    print(f"Wrote {args.output}; {sum(bool(r['failures']) for r in report['cases'])} cases need review.")
    raise SystemExit(1 if any(r['failures'] for r in report['cases']) else 0)


if __name__=='__main__': main()
