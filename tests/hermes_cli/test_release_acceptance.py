# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from hermes_cli.hussh_one_routing.release_acceptance import cases, grade
from hermes_cli.hussh_one_routing.request import Turn


@pytest.mark.parametrize('case', cases(), ids=lambda case: case['suite'])
def test_frozen_fixture_oracles_accept_exact_answer_and_reject_wrong_answer(case):
    suite = case['suite']
    if suite == 'long_context':
        correct = Turn(model='test', ok=True, content=case['expected'])
        wrong = Turn(model='test', ok=True, content='WRONG')
    else:
        name, args = {
            'terminal': ('terminal', {'command': case['expected']}),
            'tool_select': ('read_file', {'path': case['expected']}),
            'file_edit': ('patch', {'path': 'fixture.py', 'old_string': 'value = 1', 'new_string': 'value = 2'}),
        }[suite]
        correct = Turn(model='test', ok=True, tool_calls=[{'function': {'name': name, 'arguments': json.dumps(args)}}])
        wrong = Turn(model='test', ok=True, tool_calls=[])
    verdict, exact = grade(case, correct)
    assert verdict.checked and verdict.ok and exact
    verdict, exact = grade(case, wrong)
    assert not (verdict.ok and exact)
