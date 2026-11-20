"""Prevent missing/skipped checks from being reported as a passing baseline."""
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / 'scripts/improvement_eval/run.py'
spec = importlib.util.spec_from_file_location('improvement_eval', RUNNER)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


@pytest.mark.parametrize(('xml', 'expected'), [
    ('<testsuite/>', 'missing'),
    ('<testsuite><testcase classname="tests.test_x" name="test_a[one]"/></testsuite>',
     'passed'),
    ('<testsuite><testcase classname="tests.test_x" name="test_a"><skipped/>'
     '</testcase></testsuite>', 'skipped'),
    ('<testsuite><testcase classname="tests.test_x" name="test_a[one]"/>'
     '<testcase classname="tests.test_x" name="test_a[two]"><failure/>'
     '</testcase></testsuite>', 'failed'),
])
def test_check_status(xml, expected):
    records = list(ET.fromstring(xml).iter('testcase'))
    assert evaluation.check_status('tests/test_x.py::test_a', records) == expected


def test_manifest_requires_unique_cases_and_balanced_groups():
    cases = json.loads(RUNNER.with_name('cases.json').read_text())
    evaluation.validate(cases)
    cases[0]['id'] = cases[1]['id']
    with pytest.raises(ValueError, match='unique'):
        evaluation.validate(cases)


def test_core_manifest_covers_eight_areas_without_empty_checks():
    cases = json.loads(RUNNER.with_name('core_cases.json').read_text(encoding='utf-8'))
    evaluation.validate(cases, 'core')
    assert all(case['coverage'] == 'related_invariant_only' and case['offline_scope']
               for case in cases)
    cases[0]['offline_checks'] = []
    with pytest.raises(ValueError, match='require related offline checks'):
        evaluation.validate(cases, 'core')


@pytest.mark.parametrize(('results', 'expected'), [
    ({}, 'uncovered'),
    ({'a': 'passed', 'b': 'passed'}, 'passed'),
    ({'a': 'passed', 'b': 'skipped'}, 'skipped'),
    ({'a': 'missing', 'b': 'passed'}, 'missing'),
    ({'a': 'skipped', 'b': 'failed'}, 'failed'),
])
def test_case_status_does_not_turn_partial_or_absent_coverage_into_pass(results, expected):
    assert evaluation.case_status(results) == expected
