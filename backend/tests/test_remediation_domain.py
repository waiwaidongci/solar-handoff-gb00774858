"""三区补检排班的领域算法与并发边界测试。"""
import os
import tempfile
from datetime import datetime

os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'unit.db'))

from src.services.domain import (  # noqa: E402
    DEVICE_MINUTES, _free_gaps, _merge_windows, _schedule, remediation, sample_rows)


def _rows(**overrides):
    """A single-shift dataset where each zone has a plan, roster and heartbeats."""
    rows = []
    for zone, prefix in (('东区', 'E'), ('西区', 'W'), ('南区', 'S')):
        rows.append({'shift_date': '2026-11-24', 'zone': zone, 'kind': 'plan',
                     'inverter_id': '-', 'heartbeat_at': '-', 'expected_count': 3})
        for number in range(1, 4):
            rows.append({'shift_date': '2026-11-24', 'zone': zone, 'kind': 'roster',
                         'inverter_id': f'{prefix}-{number:02}', 'heartbeat_at': '-', 'expected_count': 0})
    for zone, prefix in (('东区', 'E'), ('西区', 'W'), ('南区', 'S')):
        for number in range(1, 4):
            rows.append({'shift_date': '2026-11-24', 'zone': zone, 'kind': 'duration',
                         'inverter_id': f'{prefix}-{number:02}', 'heartbeat_at': '-',
                         'expected_count': DEVICE_MINUTES[f'{prefix}-{number:02}']})
    for row in overrides.get('rows', []):
        rows.append(row)
    return rows


def _crew(zone, name, window):
    return {'shift_date': '2026-11-24', 'zone': zone, 'kind': 'crew',
            'inverter_id': name, 'heartbeat_at': window, 'expected_count': 0}


def _heartbeat(zone, device, clock):
    return {'shift_date': '2026-11-24', 'zone': zone, 'kind': 'heartbeat',
            'inverter_id': device, 'heartbeat_at': '2026-11-24T' + clock + '+08:00', 'expected_count': 0}


def test_sample_rows_flag_stale_and_missing_only():
    result = remediation(sample_rows(), {'shift_date': '2026-11-24'})
    # 只把缺心跳 / 过期的应检设备纳入；名册外的 S-99 不算。
    assert result['metrics']['needed'] == 2
    placed = {item['device'] for item in result['assignments']}
    missing = {item['device']: item['status'] for item in result['unassigned']}
    assert placed | set(missing) == {'W-03', 'S-02'}
    assert missing == {'W-03': '过期'}
    assert placed == {'S-02'}


def test_no_crew_for_zone_is_reported():
    rows = _rows(rows=[
        _heartbeat('东区', 'E-01', '06:45:00'),
        _heartbeat('西区', 'W-01', '06:45:00'),
        _heartbeat('南区', 'S-01', '06:45:00'),
        _crew('西区', '西区检修接班组', '08:00-09:00'),
    ])
    result = remediation(rows, {'shift_date': '2026-11-24'})
    reasons = {item['device']: item['reason'] for item in result['unassigned']}
    # 东区、南区没有可到的班组；西区除 W-01 外的另外两台同属“无班组能到”范畴
    # 之外的设备也一并检查。
    assert reasons['E-02'] == '无班组能到该分区'
    assert reasons['S-03'] == '无班组能到该分区'
    assert result['metrics']['needed'] == 6


def test_worktime_cannot_be_split():
    rows = _rows(rows=[
        _heartbeat('东区', 'E-01', '06:45:00'),
        _heartbeat('西区', 'W-01', '06:45:00'),
        _crew('南区', '南区检修接班组', '10:00-10:30'),
    ])
    result = remediation(rows, {'shift_date': '2026-11-24'})
    reasons = {item['device']: item['reason'] for item in result['unassigned']}
    # S-02 needs 40 minutes but the only window is 30 long; it cannot be split.
    assert reasons['S-02'] == '工时 40 分钟超过班组可用时段'
    # The 30-minute S-01 exactly fills the window; the 35-minute S-03 cannot then fit.
    placed = [item for item in result['assignments'] if item['crew'] == '南区检修接班组']
    assert [item['device'] for item in placed] == ['S-01']
    assert placed[0]['duration'] == 30


def test_same_group_never_overlaps_and_device_once():
    rows = _rows(rows=[
        _heartbeat('东区', 'E-01', '06:45:00'),
        _heartbeat('西区', 'W-01', '06:45:00'),
        _heartbeat('西区', 'W-02', '06:45:00'),
        _heartbeat('南区', 'S-01', '06:45:00'),
        _crew('西区', '机动补检组', '08:00-09:10'),
        _crew('东区', '机动补检组', '08:00-09:10'),
    ])
    result = remediation(rows, {'shift_date': '2026-11-24'})
    by_crew = {}
    for item in result['assignments']:
        by_crew.setdefault(item['crew'], []).append((item['start_minute'], item['end_minute'], item['device']))
    for items in by_crew.values():
        items.sort()
        for (_, end, _), (start, _, _) in zip(items, items[1:]):
            assert start >= end, '同一班组的安排不能重叠'
    devices = [item['device'] for item in result['assignments']]
    assert len(devices) == len(set(devices)), '同一台设备只能派一次'
    assert result['metrics']['covered'] == len(result['assignments'])


def test_claimed_assignment_is_pinned_and_rest_maximised():
    rows = _rows(rows=[
        _heartbeat('东区', 'E-01', '06:45:00'),
        _heartbeat('东区', 'E-02', '06:45:00'),
        _heartbeat('西区', 'W-01', '06:45:00'),
        _heartbeat('南区', 'S-01', '06:45:00'),
        _crew('东区', '机动补检组', '08:00-09:00'),
    ])
    pinned = [{'device': 'E-02', 'zone': '东区', 'crew': '机动补检组',
               'start_minute': 510, 'end_minute': 540, 'source': 'claimed'}]
    result = remediation(rows, {'shift_date': '2026-11-24'}, pinned)
    claimed = [item for item in result['assignments'] if item['source'] == 'claimed']
    assert claimed == [{'device': 'E-02', 'zone': '东区', 'crew': '机动补检组', 'start_minute': 510,
                        'end_minute': 540, 'duration': 30, 'source': 'claimed', 'window': '08:30-09:00'}]
    # Window is 60 minutes; E-02 (30) is pinned mid-window, E-01 (45) cannot fit either side.
    assert [item['device'] for item in result['assignments'] if item['source'] == 'planned'] == []
    assert result['metrics']['claimed'] == 1


def test_search_finds_maximum_under_ordering_pressure():
    # 40-minute window; E-01 (25) + E-02 (20) can never both fit.
    crews = {'甲组': {'zones': {'东区'}, 'windows': {'东区': [(480, 520)]}}}
    devices = [
        {'device': 'E-01', 'zone': '东区', 'duration': 25},
        {'device': 'E-02', 'zone': '东区', 'duration': 20},
    ]
    placement, capped = _schedule(devices, crews, [])
    assert not capped
    assert len(placement) == 1
    # Tie on count is broken by finishing earlier, so the shorter job is kept.
    assert placement[0][0] == 'E-02'
    # A wide window fits both.
    crews = {'甲组': {'zones': {'东区'}, 'windows': {'东区': [(480, 540)]}}}
    placement, _ = _schedule(devices, crews, [])
    assert {row[0] for row in placement} == {'E-01', 'E-02'}
    # Greedy left-to-right ordering would take E-01 first and fail; ordering search
    # still finds two jobs in an 45-minute window when picking E-02 first.
    crews = {'甲组': {'zones': {'东区'}, 'windows': {'东区': [(480, 525)]}}}
    placement, _ = _schedule(devices, crews, [])
    assert len(placement) == 2


def test_free_gaps_respect_pinned_intervals():
    gaps = _free_gaps([(480, 540)], [(500, 520)])
    assert gaps == [(480, 500), (520, 540)]
    assert _merge_windows([(480, 500), (500, 540)]) == [(480, 540)]


def test_revision_bumps_and_is_stable():
    rows = sample_rows()
    first = remediation(rows, {'shift_date': '2026-11-24'})
    second = remediation(rows, {'shift_date': '2026-11-24'})
    assert first['assignments'] == second['assignments']
