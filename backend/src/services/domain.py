import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo('Asia/Shanghai')
ZONES = ('东区', '西区', '南区')
SHIFT_START = time(6)
NOON = time(12)
HEARTBEAT_PATTERN = (
    r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?'
    r'(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?'
)
WINDOW_PATTERN = r'(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d'

# 补检资源随晨检 CSV 同行提供：crew 行给出班组能到的分区与可用时段，
# duration 行给出单台设备不可拆分的工时（分钟）。
CREWS = (
    ('西区检修接班组', ('西区',), ('08:00-08:40',)),
    ('南区检修接班组', ('南区',), ('10:00-10:45',)),
    ('机动补检组', ('东区',), ('09:00-10:00',)),
)
DEVICE_MINUTES = {
    'E-01': 45, 'E-02': 30, 'E-03': 40,
    'W-01': 35, 'W-02': 30, 'W-03': 50,
    'S-01': 30, 'S-02': 40, 'S-03': 35,
}


@dataclass
class ShiftRecords:
    expected: int | None = None
    roster: set[str] = field(default_factory=set)
    heartbeats: list[tuple[str, datetime]] = field(default_factory=list)
    crew_zones: dict[str, set[str]] = field(default_factory=dict)
    crew_windows: dict[tuple[str, str], set[tuple[int, int]]] = field(default_factory=dict)
    durations: dict[str, int] = field(default_factory=dict)


def sample_rows() -> list[dict]:
    rows = []
    for day in ('2026-11-23', '2026-11-24'):
        for zone, prefix in zip(ZONES, ('E', 'W', 'S')):
            def add(kind, inverter='-', heartbeat='-', expected=0):
                rows.append({'shift_date': day, 'zone': zone, 'kind': kind,
                             'inverter_id': inverter, 'heartbeat_at': heartbeat, 'expected_count': expected})

            add('plan', expected=3)
            incomplete = day == '2026-11-24' and zone == '南区'
            for number in range(1, 3 if incomplete else 4):
                add('roster', f'{prefix}-{number:02}')
            for number, clock in enumerate(('06:45:00', '06:56:12', '06:59:59'), start=1):
                if incomplete and number > 1:
                    continue
                if day == '2026-11-24' and zone == '西区' and number == 3:
                    clock = '06:44:59'
                add('heartbeat', f'{prefix}-{number:02}', f'{day}T{clock}+08:00')
            if incomplete:
                add('heartbeat', 'S-99', f'{day}T06:58:31+08:00')
            if day == '2026-11-24' and zone == '东区':
                add('heartbeat', 'E-02', f'{day}T06:56:54+08:00')
                add('heartbeat', 'E-01', f'{day}T07:00:00+08:00')
            for name, zones, windows in CREWS:
                if zone not in zones:
                    continue
                for window in windows:
                    add('crew', name, window)
            for number in range(1, 4):
                inverter = f'{prefix}-{number:02}'
                add('duration', inverter, '-', DEVICE_MINUTES[inverter])
    return rows


def _local_date(value: str) -> date:
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError('请将晨检日期填写为有效的 YYYY-MM-DD。')


def _local_heartbeat(value: str, day: date) -> datetime:
    if isinstance(value, str) and re.fullmatch(HEARTBEAT_PATTERN, value):
        try:
            timestamp = datetime.fromisoformat(value)
            local = timestamp.replace(tzinfo=SHANGHAI) if timestamp.tzinfo is None else timestamp.astimezone(SHANGHAI)
        except (ValueError, OverflowError):
            pass
        else:
            if local.date() != day:
                raise ValueError('心跳换算为上海时间后与晨检日期不同，请核对日期和 UTC 偏移。')
            return local
    raise ValueError('请将心跳时间填为 ISO 8601 日期和时间，例如 2026-11-24T06:59:30+08:00。')


def _parse_window(value: str) -> tuple[int, int]:
    if isinstance(value, str) and re.fullmatch(WINDOW_PATTERN, value):
        start, end = value.split('-')
        begin = time.fromisoformat(start)
        finish = time.fromisoformat(end)
        if finish > begin:
            return begin.hour * 60 + begin.minute, finish.hour * 60 + finish.minute
    raise ValueError('请将班组可用时段填为 HH:MM-HH:MM，且结束晚于开始。')


def _read_shifts(rows: list[dict]) -> dict[tuple[date, str], ShiftRecords]:
    if not rows:
        raise ValueError('请导入至少一条分区晨检记录，按班次提供计划、名册和心跳。')

    shifts = defaultdict(ShiftRecords)
    for line, row in enumerate(rows, start=2):
        try:
            day = _local_date(row['shift_date'])
            zone, kind = row['zone'], row['kind']
            inverter, timestamp, expected = row['inverter_id'], row['heartbeat_at'], row['expected_count']
            if zone not in ZONES:
                raise ValueError('请将区域改为东区、西区或南区。')
            if type(expected) is not int:
                raise ValueError('计划应检台数必须填整数。')
            shift = shifts[day, zone]
            if kind == 'plan':
                if inverter != '-' or timestamp != '-' or not 1 <= expected <= 5000:
                    raise ValueError('计划行的设备和心跳填 -，应检台数填 1 至 5000。')
                if shift.expected is not None and shift.expected != expected:
                    raise ValueError('同一分区晨班出现不同的应检台数，请按签核计划修正。')
                shift.expected = expected
            elif kind in ('roster', 'heartbeat'):
                if not isinstance(inverter, str) or not inverter.strip() or inverter == '-' or expected != 0:
                    raise ValueError('名册和心跳行须填设备编号，应检台数填 0。')
                if kind == 'roster':
                    if timestamp != '-':
                        raise ValueError('名册行的心跳时间请填 -。')
                    shift.roster.add(inverter)
                else:
                    shift.heartbeats.append((inverter, _local_heartbeat(timestamp, day)))
            elif kind == 'crew':
                if not isinstance(inverter, str) or not inverter.strip() or inverter == '-' or expected != 0:
                    raise ValueError('班组行的设备编号填班组名称，应检台数填 0。')
                window = _parse_window(timestamp)
                shift.crew_zones.setdefault(inverter.strip(), set()).add(zone)
                shift.crew_windows.setdefault((inverter.strip(), zone), set()).add(window)
            elif kind == 'duration':
                if not isinstance(inverter, str) or not inverter.strip() or inverter == '-':
                    raise ValueError('工时行须填设备编号，应检台数填单台工时。')
                if timestamp != '-':
                    raise ValueError('工时行的心跳时间请填 -。')
                if not 1 <= expected <= 1440:
                    raise ValueError('单台设备工时须为 1 至 1440 分钟的整数。')
                previous = shift.durations.get(inverter)
                if previous is not None and previous != expected:
                    raise ValueError(f'{inverter} 出现不同的工时，请核对设备台账。')
                shift.durations[inverter] = expected
            else:
                raise ValueError('请将记录类型改为 plan、roster、heartbeat、crew 或 duration。')
        except KeyError as error:
            raise ValueError(f'CSV 第 {line} 行缺少字段 {error.args[0]}，请使用示例表头补齐。') from error
        except ValueError as error:
            raise ValueError(f'CSV 第 {line} 行：{error}') from error
    return shifts


def _window_end(options: dict) -> time:
    value = options.get('cutoff', '07:00')
    if isinstance(value, str) and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        cutoff = time.fromisoformat(value)
        if SHIFT_START < cutoff <= NOON:
            return cutoff
    raise ValueError('请将上海晨检截点填写为 06:01 至 12:00 间的 HH:MM。')


def _latest_heartbeats(heartbeats: list[tuple[str, datetime]], start: datetime, end: datetime):
    per_minute = {}
    duplicates = outside = 0
    for inverter, timestamp in heartbeats:
        if not start <= timestamp < end:
            outside += 1
            continue
        minute = timestamp.replace(second=0, microsecond=0)
        key = inverter, minute
        previous = per_minute.get(key)
        if previous is not None:
            duplicates += 1
        if previous is None or timestamp > previous:
            per_minute[key] = timestamp

    latest = {}
    for (inverter, minute), timestamp in per_minute.items():
        if inverter not in latest or timestamp > latest[inverter][1]:
            latest[inverter] = (minute, timestamp)
    return latest, duplicates, outside


def _evaluate(shift: ShiftRecords, start: datetime, end: datetime, freshness: int):
    latest, duplicates, outside = _latest_heartbeats(shift.heartbeats, start, end)
    counts = {'新鲜': 0, '过期': 0, '缺心跳': 0, '名册外': 0}
    details = []
    for inverter in sorted(shift.roster | latest.keys()):
        minute, timestamp = latest.get(inverter, (None, None))
        age = int((end - minute).total_seconds() // 60) if minute is not None else None
        if inverter not in shift.roster:
            status = '名册外'
        elif age is None:
            status = '缺心跳'
        elif age > freshness:
            status = '过期'
        else:
            status = '新鲜'
        counts[status] += 1
        details.append({'逆变器': inverter, '心跳状态': status,
                        '最后心跳（上海）': timestamp.isoformat() if timestamp else None,
                        '心跳归属分钟（上海）': minute.isoformat(timespec='minutes') if minute else None,
                        '距截点分钟': age})
    blockers = []
    if shift.expected is None:
        blockers.append('缺应检计划')
    elif len(shift.roster) != shift.expected:
        blockers.append(f'名册 {len(shift.roster)}/{shift.expected} 台')
    if counts['缺心跳']:
        blockers.append(f"{counts['缺心跳']} 台缺心跳")
    if counts['过期']:
        blockers.append(f"{counts['过期']} 台心跳过期")
    return counts, details, blockers, duplicates, outside


def compute(rows: list[dict], options: dict) -> dict:
    zone = options.get('zone', ZONES[0])
    if zone not in ZONES:
        raise ValueError('请选择东区、西区或南区作为交班报告范围。')
    freshness = options.get('freshness_minutes', 15)
    if type(freshness) is not int or not 1 <= freshness <= 120:
        raise ValueError('新鲜度上限请填写 1 至 120 的整数分钟。')
    cutoff = _window_end(options)
    shifts = _read_shifts(rows)
    requested_date = options.get('shift_date', '最新')
    day = max(record_day for record_day, _ in shifts) if requested_date == '最新' else _local_date(requested_date)
    shift = shifts.get((day, zone), ShiftRecords())
    start = datetime.combine(day, SHIFT_START, SHANGHAI)
    end = datetime.combine(day, cutoff, SHANGHAI)
    counts, details, blockers, duplicates, outside = _evaluate(shift, start, end, freshness)

    reason = '、'.join(blockers)
    gate = f'拦截 · {reason}' if blockers else '可发布'
    if not details:
        details.append({'逆变器': None, '心跳状态': '无设备明细', '最后心跳（上海）': None,
                        '心跳归属分钟（上海）': None, '距截点分钟': None})
    report_rows = [
        {'晨检日期': day.isoformat(), '区域': zone, '发布门禁': gate,
         '计划应检台数': shift.expected, '去重名册台数': len(shift.roster),
         '截点（上海）': end.isoformat(timespec='minutes'), **row}
        for row in details
    ]
    metrics = {'计划应检台数': shift.expected if shift.expected is not None else '未填报',
               '去重名册台数': len(shift.roster), '新鲜台数': counts['新鲜'], '过期台数': counts['过期'],
               '缺心跳台数': counts['缺心跳'], '名册外设备数': counts['名册外'],
               '同分钟去重条数': duplicates, '窗口外心跳条数': outside}
    notes = [
        f'截点按 Asia/Shanghai 解释。窗口从 06:00 含起点，到 {cutoff:%H:%M} 不含截点。',
        f'同一班次、区域、设备和本地分钟只留最晚一条。截点减最后心跳归属分钟不超过 {freshness} 分钟即为新鲜。',
        '班前签核计划给出应检台数，名册提供逐台预期设备。名册外心跳单列，无法补齐名册或替代应检设备。',
        '每个分区晨班独立复核并生成报告。批准后在通用分发页为对应值班组建立单次投递。',
    ]
    if blockers:
        notes.append('请向值班长核对应检计划和设备名册，补传窗口内的有效心跳后重新计算。')
    headline = f'{day.isoformat()} {zone}晨班｜拦截：{reason}' if blockers else (
        f"{day.isoformat()} {zone}晨班｜{counts['新鲜']} 台应检逆变器齐备且心跳新鲜，可发布"
    )
    return {'headline': headline, 'can_publish': not blockers, 'metrics': metrics, 'rows': report_rows,
            'notes': notes, 'dispatch_recipient': f'{zone}检修接班组', 'chart': {'label': f'{zone}晨班应检设备心跳状态', 'unit': '台',
                                     'points': [{'label': status, 'value': counts[status]}
                                                for status in ('新鲜', '过期', '缺心跳')]}}


def resolve_day(rows: list[dict], options: dict) -> date:
    shifts = _read_shifts(rows)
    requested = options.get('shift_date', '最新')
    day = max(record_day for record_day, _ in shifts) if requested == '最新' else _local_date(requested)
    if not any(record_day == day for record_day, _ in shifts):
        raise ValueError(f'数据中没有 {day.isoformat()} 的晨检记录。')
    return day


def _merge_windows(windows):
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _clock(minutes: int) -> str:
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def _free_gaps(windows, occupancy):
    """Free sub-intervals of a crew's windows after its pinned assignments."""
    gaps = []
    for window_start, window_end in windows:
        cursor = window_start
        for start, end in sorted(occupancy):
            if end <= cursor:
                continue
            if start >= window_end:
                break
            if start > cursor:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if window_end > cursor:
            gaps.append((cursor, window_end))
    return gaps


def _schedule(devices, crews, claimed, budget=300_000):
    """Maximise the number of devices placed without overlap.

    Each device is left-aligned in a free gap of a crew that can reach its zone.
    The search picks which remaining device to place next, so it covers every
    ordering; for a fixed set of placements, left-aligning every job is
    dominant, so the maximum cardinality is found exactly for small inputs.
    Nodes are capped so a malformed upload cannot pin a worker forever; the
    best-so-far plan is returned when the cap is hit.
    """
    occupancy = {name: [] for name in crews}
    for item in claimed:
        occupancy.setdefault(item['crew'], []).append((item['start_minute'], item['end_minute']))
    for name in occupancy:
        occupancy[name].sort()

    def eligible(device):
        return [name for name, crew in crews.items()
                if device['zone'] in crew['zones'] and crew['windows'].get(device['zone'])]

    devices = sorted(devices, key=lambda item: (len(eligible(item)), -item['duration'],
                                                item['zone'], item['device']))
    best = {'covered': -1, 'spread': 0, 'placement': []}
    nodes = 0
    capped = False

    def record(current):
        spread = sum(end for _, _, _, end in current)
        if len(current) > best['covered'] or (len(current) == best['covered'] and spread < best['spread']):
            best.update(covered=len(current), spread=spread, placement=list(current))

    def walk(remaining, current):
        nonlocal nodes, capped
        nodes += 1
        if nodes > budget:
            capped = True
            return
        record(current)
        if capped or len(current) + len(remaining) <= best['covered']:
            return
        for index, device in enumerate(remaining):
            for crew_name in eligible(device):
                crew = crews[crew_name]
                for gap_start, gap_end in _free_gaps(crew['windows'][device['zone']], occupancy[crew_name]):
                    duration = device['duration']
                    if gap_end - gap_start < duration:
                        continue
                    segment = (gap_start, gap_start + duration)
                    occupancy[crew_name].append(segment)
                    occupancy[crew_name].sort()
                    current.append((device['device'], crew_name, segment[0], segment[1]))
                    walk(remaining[:index] + remaining[index + 1:], current)
                    current.pop()
                    occupancy[crew_name].remove(segment)
                    if capped:
                        return
            if capped:
                return

    walk(list(devices), [])
    if best['covered'] < 0:
        best.update(covered=0, spread=0, placement=[])
    return best['placement'], capped


def remediation(rows: list[dict], options: dict, fixed: list[dict] | None = None) -> dict:
    """Combine the three zones' stale or missing-heartbeat devices into one shift plan.

    ``fixed`` holds assignments that must not move: crew-accepted orders and
    already-published dispatches. They pin their interval before the search runs
    and their devices are never scheduled a second time.
    """
    freshness = options.get('freshness_minutes', 15)
    if type(freshness) is not int or not 1 <= freshness <= 120:
        raise ValueError('新鲜度上限请填写 1 至 120 的整数分钟。')
    cutoff = _window_end(options)
    shifts = _read_shifts(rows)
    requested = options.get('shift_date', '最新')
    day = max(record_day for record_day, _ in shifts) if requested == '最新' else _local_date(requested)
    if not any(record_day == day for record_day, _ in shifts):
        raise ValueError(f'数据中没有 {day.isoformat()} 的晨检记录。')
    start = datetime.combine(day, SHIFT_START, SHANGHAI)
    end = datetime.combine(day, cutoff, SHANGHAI)

    by_device = {}
    for item in fixed or []:
        if item['device'] not in by_device or item.get('source') == 'published':
            by_device[item['device']] = item
    pinned = []
    for item in by_device.values():
        pinned.append({'device': item['device'], 'zone': item.get('zone', ''),
                       'crew': item['crew'], 'start_minute': item['start_minute'],
                       'end_minute': item['end_minute'],
                       'duration': item['end_minute'] - item['start_minute'],
                       'source': item.get('source', 'claimed')})
    claimed_devices = {item['device'] for item in pinned}

    needed = []
    for zone in ZONES:
        shift = shifts.get((day, zone), ShiftRecords())
        _, details, _, _, _ = _evaluate(shift, start, end, freshness)
        for row in details:
            if row['逆变器'] is not None and row['心跳状态'] in ('缺心跳', '过期'):
                needed.append({'device': row['逆变器'], 'zone': zone, 'status': row['心跳状态']})
    needed.sort(key=lambda item: (item['zone'], item['device']))

    durations = {}
    for zone in ZONES:
        shift = shifts.get((day, zone), ShiftRecords())
        for device, minutes in shift.durations.items():
            previous = durations.get(device)
            if previous is not None and previous != minutes:
                raise ValueError(f'{device} 出现不同的工时，请核对设备台账。')
            durations[device] = minutes

    crew_zones, crew_windows = {}, {}
    for zone in ZONES:
        shift = shifts.get((day, zone), ShiftRecords())
        for name, zones in shift.crew_zones.items():
            crew_zones.setdefault(name, set()).update(zones)
        for (name, crew_zone), windows in shift.crew_windows.items():
            crew_windows.setdefault((name, crew_zone), set()).update(windows)
    crews = {}
    for name in sorted(crew_zones):
        zones = crew_zones[name]
        crews[name] = {
            'zones': set(zones),
            'windows': {zone: _merge_windows(crew_windows.get((name, zone), set())) for zone in sorted(zones)},
        }

    pending = [item for item in needed if item['device'] not in claimed_devices]
    unassignable = []
    candidates = []
    for item in pending:
        minutes = durations.get(item['device'])
        if minutes is None:
            unassignable.append({**item, 'reason': '缺设备工时，无法排班'})
            continue
        eligible = [name for name, crew in crews.items()
                    if item['zone'] in crew['zones'] and crew['windows'].get(item['zone'])]
        if not eligible:
            unassignable.append({**item, 'reason': '无班组能到该分区'})
            continue
        longest = max((finish - begin for name in eligible
                       for begin, finish in crews[name]['windows'][item['zone']]), default=0)
        if longest < minutes:
            unassignable.append({**item, 'reason': f'工时 {minutes} 分钟超过班组可用时段'})
            continue
        candidates.append({**item, 'duration': minutes})

    placement, capped = _schedule(candidates, crews, pinned)
    for device, crew, begin, finish in placement:
        pending_item = next(item for item in candidates if item['device'] == device)
        pinned.append({'device': device, 'zone': pending_item['zone'], 'crew': crew,
                       'start_minute': begin, 'end_minute': finish,
                       'duration': finish - begin, 'source': 'planned'})

    scheduled_devices = {item['device'] for item in pinned}
    placed_devices = {device for device, _, _, _ in placement}
    unassigned = list(unassignable)
    unassignable_devices = {item['device'] for item in unassignable}
    for item in pending:
        if item['device'] in placed_devices or item['device'] in unassignable_devices:
            continue
        reason = ('排班搜索达到上限，暂未排上' if capped
                  else '可用时段已被占满或与已接单安排冲突')
        unassigned.append({**item, 'reason': reason})
    unassigned.sort(key=lambda item: (item['zone'], item['device']))

    assignments = sorted(pinned, key=lambda item: (item['start_minute'], item['crew'], item['device']))
    for item in assignments:
        item['window'] = f"{_clock(item['start_minute'])}-{_clock(item['end_minute'])}"
    crew_rows = []
    for name, crew in crews.items():
        items = [item for item in assignments if item['crew'] == name]
        crew_rows.append({
            'crew': name,
            'zones': [zone for zone in ZONES if zone in crew['zones']],
            'windows': [f"{zone} {'、'.join(f'{_clock(begin)}-{_clock(finish)}' for begin, finish in crew['windows'][zone])}"
                        for zone in ZONES if crew['windows'].get(zone)],
            'minutes': sum(item['duration'] for item in items),
            'claimed': sum(1 for item in items if item['source'] == 'claimed'),
            'published': sum(1 for item in items if item['source'] == 'published'),
            'proposed': sum(1 for item in items if item['source'] == 'planned'),
        })
    for item in assignments:
        if item['crew'] not in crews:
            crew_rows.append({'crew': item['crew'], 'zones': [item['zone']], 'windows': [],
                              'minutes': item['duration'], 'claimed': int(item['source'] == 'claimed'),
                              'published': int(item['source'] == 'published'), 'proposed': 0})
    crew_rows.sort(key=lambda row: row['crew'])

    needed_devices = {item['device'] for item in needed}
    covered = len(needed_devices & scheduled_devices)
    metrics = {'needed': len(needed_devices), 'covered': covered,
               'claimed': sum(1 for item in assignments if item['source'] == 'claimed'),
               'published': sum(1 for item in assignments if item['source'] == 'published'),
               'planned': sum(1 for item in assignments if item['source'] == 'planned'),
               'unassigned': len(needed_devices - scheduled_devices),
               'crew_count': len(crew_rows)}
    if metrics['needed'] == 0:
        headline = f'{day.isoformat()} 补检排班｜三区晨班无缺心跳或过期设备'
    else:
        headline = (f"{day.isoformat()} 补检排班｜已排 {covered}/{metrics['needed']} 台"
                    f"，{metrics['unassigned']} 台未排上")
    notes = [
        '补检台数取自同班三区名册中缺心跳或心跳过期的应检设备；名册外设备不纳入排班。',
        '班组只能承接其能到的分区，安排须落在其可用时段内；单台设备工时整段占用，不可拆分。',
        '同一班组的安排互不重叠，同一台设备在本班只派一次。已接单和已发布的安排固定不动，其余重算以本班排上台数最多为目标。',
        f'新鲜度与截点沿用晨检口径：{cutoff:%H:%M} 前 {freshness} 分钟内为新鲜。',
    ]
    if capped:
        notes.append('设备较多，排班搜索达到上限；未排上的设备可能仍可人工补排。')
    return {'shift_date': day.isoformat(), 'headline': headline, 'notes': notes,
            'metrics': metrics, 'crews': crew_rows, 'assignments': assignments,
            'unassigned': unassigned, 'cutoff': f'{cutoff:%H:%M}', 'freshness_minutes': freshness}
