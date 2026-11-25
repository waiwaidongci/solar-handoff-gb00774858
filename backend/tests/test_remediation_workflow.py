"""补检排班的持久化边界：接单与重算并发、发布重试不重复派单。"""
import os
import tempfile
import threading

os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'workflow.db')

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from src.models.database import engine  # noqa: E402
from src.models.tables import metadata  # noqa: E402
from src.services.data import PROFILE, convert, save_revision  # noqa: E402
from src.services.domain import sample_rows  # noqa: E402
from src.services.workflow import (claim_remediation, create_remediation, publish_remediation,  # noqa: E402
                                   recompute_remediation, release_remediation, remediation_detail)


@pytest.fixture()
def plan():
    metadata.drop_all(engine)
    metadata.create_all(engine)
    records = [{field['key']: convert(row[field['key']], field) for field in PROFILE['columns']}
               for row in sample_rows()]
    dataset_id, _ = save_revision('demo.csv', records, 'tester')
    created = create_remediation(dataset_id, {}, 'tester')
    return created['id']


def sources(plan_id):
    return {item['device']: item['source'] for item in remediation_detail(plan_id)['assignments']}


def test_create_is_stable_and_recomputes_claim(plan):
    first = remediation_detail(plan)
    assert first['revision'] == 1
    assert sources(plan) == {'S-02': 'planned'}
    claim_remediation(plan, 'S-02', 'worker-a')
    assert sources(plan) == {'S-02': 'claimed'}
    recompute_remediation(plan, 'worker-b')
    # A recompute must never move or drop an accepted order.
    detail = remediation_detail(plan)
    assert sources(plan) == {'S-02': 'claimed'}
    assert detail['revision'] == 2


def test_claim_is_idempotent(plan):
    first = claim_remediation(plan, 'S-02', 'worker-a')
    second = claim_remediation(plan, 'S-02', 'worker-b')
    assert first['device'] == second['device'] == 'S-02'
    assert first['start_minute'] == second['start_minute']
    claims = remediation_detail(plan)['claims']
    assert len(claims) == 1


def test_claim_rejects_unscheduled_device(plan):
    with pytest.raises(HTTPException) as error:
        claim_remediation(plan, 'W-03', 'worker-a')
    assert error.value.status_code == 409


def test_publish_is_idempotent_across_retry(plan):
    claim_remediation(plan, 'S-02', 'worker-a')
    first = publish_remediation(plan, 'reviewer')
    assert first['created'] == 1
    retry = publish_remediation(plan, 'reviewer')
    assert retry['created'] == 0
    published = remediation_detail(plan)['published']
    assert [row['device'] for row in published] == ['S-02']
    assert sources(plan)['S-02'] == 'published'


def test_publish_rejects_stale_revision(plan):
    with pytest.raises(HTTPException) as error:
        publish_remediation(plan, 'reviewer', expected_revision=99)
    assert error.value.status_code == 409


def test_published_order_cannot_be_released(plan):
    claim_remediation(plan, 'S-02', 'worker-a')
    publish_remediation(plan, 'reviewer')
    with pytest.raises(HTTPException) as error:
        release_remediation(plan, 'S-02', 'worker-a')
    assert error.value.status_code == 409


def test_concurrent_claim_and_recompute_keeps_order(plan):
    """A claim racing a recompute must survive either interleaving."""
    claim_remediation(plan, 'S-02', 'worker-a')
    failures = []

    def claimer():
        try:
            claim_remediation(plan, 'S-02', 'worker-b')
        except Exception as error:  # pragma: no cover - surfaced by the assert below
            failures.append(error)

    def recomputer():
        try:
            recompute_remediation(plan, 'worker-c')
        except Exception as error:  # pragma: no cover
            failures.append(error)

    threads = [threading.Thread(target=claimer), threading.Thread(target=recomputer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures
    assert sources(plan)['S-02'] == 'claimed'
    assert len(remediation_detail(plan)['claims']) == 1
