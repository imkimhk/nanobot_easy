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
