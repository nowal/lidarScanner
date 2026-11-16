"""Opt-in synthetic generation benchmark. Never imports API or delivery code.

Compare identical synthetic states against a trusted local git revision. Reports
wall time, model calls/tokens, cache usage and quality checks; never calls a
submission endpoint or reads customer conversations.
"""
import argparse
import asyncio
import copy
import json
import logging
from pathlib import Path
import re
import subprocess
import time
import types

from app.config import settings
from app.quote_intake import conversation, engine, provider


def baseline(ref):
    modules = {}
    for name in ('engine', 'provider', 'conversation'):
        source = subprocess.check_output(['git', 'show', f'{ref}:backend/app/quote_intake/{name}.py'], text=True)
        module = types.ModuleType(f'app.quote_intake.benchmark_baseline_{name}')
        module.__package__ = 'app.quote_intake'
        exec(compile(source, f'<baseline:{name}>', 'exec'), module.__dict__)
        modules[name] = module
    modules['conversation'].engine = modules['engine']
    modules['conversation'].provider = modules['provider']
    return modules['conversation']


def states():
    cases = []
    for name, text in [
        ('short_clarifier', 'Paint my living room.'),
        ('complete_scope', 'Paint the living room walls only, full repaint, you choose the finish, ZIP 01234.'),
        ('long_saved_thread', 'Use the provider recommendation for the finish. The project ZIP is 01234.')]:
        state = engine.initial_state()
        if name == 'long_saved_thread':
            # Every prior fact remains available; no tail-window truncation.
            for n in range(1, 26):
                state['transcript'].extend([
                    dict(id=f'old-{n}', role='homeowner', text='Paint the living room walls only, full repaint. Keep the wood trim and ceiling excluded.'),
                    dict(id=f'old-{n}-reply', role='assistant', text='The living room walls are in scope; trim and ceiling stay excluded.')])
        state['transcript'].append(dict(id='latest', role='homeowner', text=text,
                                       receivedAt='2026-11-13T15:00:00+00:00', timeZone='America/New_York'))
        state['pendingTurn'] = {'clientTurnId': 'latest'}
        cases.append((name, state))
    return cases


class Usage(logging.Handler):
    def __init__(self):
        super().__init__()
        self.calls = []
    def emit(self, record):
        message = record.getMessage()
        if not message.startswith('quote_intake model='):
            return
        fields = dict(re.findall(r'(model|input_tokens|output_tokens|seconds|cache_read_tokens|cache_write_tokens|review)=([^ ]+)', message))
        for key in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'):
            fields[key] = int(fields.get(key, 0) or 0)
        fields['seconds'] = float(fields['seconds'])
        self.calls.append(fields)


async def run(args):
    variants = {'baseline': baseline(args.compare_ref), 'optimized': conversation}
    usage = Usage()
    root = logging.getLogger(); root.addHandler(usage); root.setLevel(logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpx2').setLevel(logging.WARNING)
    samples = []
    for repeat in range(args.repeats):
        for case, state in states():
            order = list(variants) if repeat % 2 == 0 else list(reversed(variants))
            for variant in order:
                usage.calls = []; attempts = []
                started = time.perf_counter()
                try:
                    output = await variants[variant].generate_turn(copy.deepcopy(state), 'America/New_York', attempts=attempts)
                    failures = []
                    if case == 'short_clarifier':
                        if output.get('ready') or not output['result']['nextQuestion']: failures.append('missing_clarifier')
                    else:
                        if not output.get('ready') or output['projectZip'] != '01234': failures.append('scope_or_zip_lost')
                    # apply_result already verifies every citation and old answer.
                    sample = dict(repeat=repeat, case=case, variant=variant,
                        seconds=round(time.perf_counter()-started, 3), calls=list(usage.calls), attempts=attempts,
                        failures=failures, result=output['result'], workItems=output['workItems'])
                except Exception as exc:
                    sample = dict(repeat=repeat, case=case, variant=variant,
                        seconds=round(time.perf_counter()-started, 3), calls=list(usage.calls),
                        failures=[type(exc).__name__])
                samples.append(sample)
                print(f'{variant} {case} #{repeat+1}: {sample["seconds"]}s calls={len(sample["calls"])} failures={sample["failures"]}', flush=True)
                args.output.write_text(json.dumps(dict(model=settings.anthropic_model, effort=settings.anthropic_effort,
                    maxTokens=settings.anthropic_max_tokens, baseline=args.compare_ref, submissionCalls=0,
                    notes='Synthetic generation only; excludes phone, network to backend, auth, database and rendering. Cache warmth is observed in per-call token counters; no population percentiles.',
                    samples=samples), indent=2))
    return any(sample['failures'] for sample in samples)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--compare-ref', required=True)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live: parser.error('--live explicitly enables synthetic model calls')
    if not settings.anthropic_api_key: parser.error('Anthropic credentials unavailable')
    if not 1 <= args.repeats <= 5: parser.error('--repeats must be between 1 and 5')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(run(args)))
