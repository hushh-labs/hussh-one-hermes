# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Frozen synthetic release cases composed from the existing Puppy exam oracles.

This is a narrow release regression receipt, not a general model-quality score.
No model-proposed command or edit is executed. Runtime/UI proofs are separate.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

from hermes_cli.hussh_one_routing import request
from hermes_cli.hussh_one_routing.exam import file_edit, long_context, terminal, tool_select
from hermes_cli.hussh_one_routing.local_runtime import LocalInferenceAdmission

MODEL = 'meta/muse-glimmer'
ENDPOINT = 'http://127.0.0.1:1234'
SETTINGS = {'max_tokens': 2048, 'reasoning_effort': 'low', 'temperature': 0.0, 'timeout': 120}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def tool(name, properties):
    return {'type': 'function', 'function': {'name': name, 'description': name,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': list(properties), 'additionalProperties': False}}}


def cases():
    needle, token = long_context.make_needle('hermes-release-v1-20260914')
    filler = '\n'.join(f'Synthetic ledger row {i}: routine inventory record; no audit reference.' for i in range(3000))
    return [
        {'suite': 'terminal', 'prompt': "Call terminal once with exactly this command: printf 'HERMES_RELEASE_OK\\n'. Do not add other commands.",
         'tools': [tool('terminal', {'command': {'type': 'string'}})],
         'expected': "printf 'HERMES_RELEASE_OK\\n'"},
        {'suite': 'tool_select', 'prompt': 'Read the file notes.txt. Call read_file exactly once with path notes.txt.',
         'tools': [tool('read_file', {'path': {'type': 'string'}}), tool('list_directory', {'path': {'type': 'string'}})],
         'expected': 'notes.txt'},
        {'suite': 'file_edit', 'prompt': 'The complete current contents of fixture.py are: value = 1\nChange only 1 to 2 using patch once with path, old_string and new_string.',
         'tools': [tool('patch', {k: {'type': 'string'} for k in ('path', 'old_string', 'new_string')})],
         'pre': 'value = 1\n', 'expected': 'value = 2\n'},
        {'suite': 'long_context', 'prompt': long_context.plant(filler, needle, 0.75) + '\nReturn only the exact internal audit reference from these records.',
         'tools': None, 'expected': token},
    ]


def model_snapshot():
    with urllib.request.urlopen(ENDPOINT + '/api/v1/models', timeout=5) as response:
        models = json.load(response).get('models', [])
    matches = [m for m in models if m.get('key') == MODEL]
    if len(matches) != 1 or len(matches[0].get('loaded_instances', [])) != 1:
        raise RuntimeError('Pinned model must already have exactly one loaded instance')
    instance = matches[0]['loaded_instances'][0]
    if instance.get('id') != MODEL:
        raise RuntimeError('Loaded instance identity differs from pinned model')
    return {'model': MODEL, 'loaded_context': instance['config']['context_length'],
            'configuration': instance['config'],
            'artifact': {key: matches[0].get(key) for key in ('architecture', 'quantization', 'size_bytes', 'format', 'selected_variant')},
            'model_revision': 'not_reported_by_provider'}


def grade(case, turn):
    suite = case['suite']
    if suite == 'long_context':
        verdict = long_context.grade_needle(case_id=suite, answer=turn.content, token=case['expected'])
        exact = turn.content.strip() == case['expected']
    else:
        call = turn.tool_calls[0] if len(turn.tool_calls) == 1 else {}
        function = call.get('function', {})
        name = function.get('name')
        args = file_edit.parse_arguments(function.get('arguments'))
        if suite == 'terminal':
            verdict = terminal.grade(case_id=suite, args=args, instruction=case['prompt'])
            exact = name == 'terminal' and args == {'command': case['expected']}
        elif suite == 'tool_select':
            schemas = {t['function']['name']: t['function']['parameters'] for t in case['tools']}
            verdict = tool_select.grade(case_id=suite, chosen=name, arguments=args,
                                       expected='read_file', catalog=list(schemas), schemas=schemas)
            exact = args == {'path': case['expected']}
        else:
            verdict = file_edit.grade(case_id=suite, path='fixture.py', args=args, pre=case['pre'],
                                      actual=case['expected'], context={'last_read_partial': False})
            exact = name == 'patch' and args.get('path') == 'fixture.py' and file_edit.post_image(args, case['pre']) == case['expected']
    return verdict, exact


def run(output: Path, repetitions=3):
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True, encoding='utf-8').strip()
    if subprocess.check_output(['git', 'diff', 'HEAD', '--name-only'], text=True, encoding='utf-8').strip():
        raise RuntimeError('Acceptance requires a clean tracked checkout')
    fixtures = cases()
    snapshot = model_snapshot()
    manifest = {'settings': SETTINGS, 'cases': fixtures, 'provider': 'lmstudio', 'endpoint': ENDPOINT,
                'model': MODEL, 'repetitions': repetitions}
    receipt = {'schema': 1, 'scope': 'synthetic_exam_cases_only', 'revision': revision,
               'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'manifest_sha256': fingerprint(manifest), 'configuration_sha256': fingerprint(snapshot),
               'model': snapshot, 'expected_cases': len(fixtures) * repetitions,
               'cases': [], 'complete': False, 'passed': False}
    output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        output.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    save()
    for case in fixtures:
        for repeat in range(repetitions):
            row = {'suite': case['suite'], 'repeat': repeat + 1, 'status': 'incomplete'}
            receipt['cases'].append(row)
            save()
            served = {}
            def send(req, *, timeout):
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    body = response.read()
                payload = json.loads(body)
                served['model'] = payload.get('model')
                return io.BytesIO(body)
            queued = time.monotonic()
            lease = LocalInferenceAdmission.acquire(ENDPOINT + '/v1|' + MODEL, wait=120, priority='interactive')
            row['queue_ms'] = round((time.monotonic() - queued) * 1000, 2)
            try:
                turn = request.complete(model=MODEL, base_url=ENDPOINT,
                    messages=[{'role': 'user', 'content': case['prompt']}], tools=case['tools'],
                    opener=send, **SETTINGS)
            finally:
                lease.release()
            row.update(latency_ms=turn.elapsed_ms, completion_tokens=turn.completion_tokens,
                       finish_reason=turn.finish_reason, response_model=served.get('model'))
            if served.get('model') != MODEL or model_snapshot() != snapshot:
                row['status'] = 'model_changed'
                save()
                return receipt
            if turn.indeterminate:
                row['status'] = 'incomplete' if turn.truncated else 'harness_fault'
                row['timed_out'] = turn.timed_out
            else:
                verdict, exact = grade(case, turn)
                row['outcomes'] = [asdict(o) for o in verdict.outcomes]
                row['exact_fixture_match'] = exact
                row['status'] = 'passed' if verdict.ok and verdict.checked and exact else 'model_failure'
            save()
            print(f"{case['suite']} repetition={repeat + 1} status={row['status']}", flush=True)
    receipt['complete'] = len(receipt['cases']) == receipt['expected_cases'] and all(r['status'] in {'passed', 'model_failure'} for r in receipt['cases'])
    receipt['passed'] = receipt['complete'] and all(r['status'] == 'passed' for r in receipt['cases'])
    save()
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.output)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
