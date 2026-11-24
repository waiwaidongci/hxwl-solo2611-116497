"""Work-order variance, drivers and reconciliation for the maintenance ledger."""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation


DIMENSIONS = {'班次': 'shift', '承包商': 'contractor'}
MISSING = '（未分类）'
CENT = Decimal('0.01')
LIMIT = 10000


def _text(value, label, limit, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{label}必须是文本')
    value = value.strip()
    if not value and required:
        raise ValueError(f'{label}不能为空')
    if len(value) > limit:
        raise ValueError(f'{label}最多 {limit} 个字符')
    return value or None


def _number(value, label, step):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f'{label}必须是有效数字') from None
    if not number.is_finite() or not 0 <= number <= LIMIT:
        raise ValueError(f'{label}必须在 0 到 {LIMIT} 之间')
    if number != number.quantize(step):
        precision = '必须为整数' if step == 1 else '最多保留两位小数'
        raise ValueError(f'{label}{precision}')
    return int(number / step)


def _hours(cents):
    return cents / 100


def _rounded_share(numerator, denominator):
    whole, remainder = divmod(abs(numerator), denominator)
    rounded = whole + (2 * remainder >= denominator)
    return rounded if numerator >= 0 else -rounded


def _category(value):
    if value is None:
        return MISSING
    return f'录入值「{value}」' if value == MISSING else value


@dataclass(frozen=True)
class WorkOrder:
    order_id: str
    work_date: str
    asset_id: str
    asset_type: str | None
    shift: str | None
    contractor: str | None
    planned: int
    completed: int
    budget: int
    actual: int

    @classmethod
    def parse(cls, row):
        work_date = _text(row.get('work_date'), '作业日期', 10)
        try:
            work_date = date.fromisoformat(work_date).isoformat()
        except ValueError:
            raise ValueError('作业日期必须是有效的 ISO 日期') from None
        order = cls(
            _text(row.get('order_id'), '工单号', 80),
            work_date,
            _text(row.get('asset_id'), '资产编号', 80),
            _text(row.get('asset_type'), '设备类型', 60, required=False),
            _text(row.get('shift'), '班次', 60, required=False),
            _text(row.get('contractor'), '承包商／自维班', 60, required=False),
            _number(row.get('planned_units'), '计划作业点数', Decimal(1)),
            _number(row.get('actual_units'), '完成作业点数', Decimal(1)),
            _number(row.get('budget_hours'), '预算工时', CENT),
            _number(row.get('actual_hours'), '实耗工时', CENT),
        )
        if order.budget and not order.planned:
            raise ValueError('有预算工时的工单必须填写正数计划作业点数')
        return order

    def totals(self):
        if self.budget == 0:
            return Totals(1, 0, self.actual, 0, 0, self.actual)
        volume = _rounded_share((self.completed - self.planned) * self.budget, self.planned)
        return Totals(1, self.budget, self.actual, volume, self.actual - self.budget - volume, 0)

    def detail(self, group_id, totals):
        return [
            group_id, self.order_id, self.work_date, self.asset_id,
            _category(self.asset_type), _category(self.shift), _category(self.contractor),
            self.planned, self.completed, *totals.hours_row(),
        ]


@dataclass
class Totals:
    count: int = 0
    budget: int = 0
    actual: int = 0
    volume: int = 0
    efficiency: int = 0
    unbudgeted: int = 0

    @property
    def variance(self):
        return self.actual - self.budget

    @property
    def percent(self):
        return round(self.variance * 100 / self.budget, 4) if self.budget else None

    def add(self, other):
        self.count += other.count
        self.budget += other.budget
        self.actual += other.actual
        self.volume += other.volume
        self.efficiency += other.efficiency
        self.unbudgeted += other.unbudgeted

    def difference(self, other):
        return Totals(
            self.count - other.count, self.budget - other.budget, self.actual - other.actual,
            self.volume - other.volume, self.efficiency - other.efficiency, self.unbudgeted - other.unbudgeted,
        )

    def hours_row(self):
        return [_hours(value) for value in (
            self.budget, self.actual, self.variance, self.volume, self.efficiency, self.unbudgeted,
        )]


HOUR_COLUMNS = ['预算（人时）', '实耗（人时）', '净差（人时）', '作业量影响（人时）',
                '单耗及其他（人时）', '无预算用工（人时）']

CHECKLIST_COLUMNS = ['序号', '工单号', '作业日期', '资产编号', '设备类型', '班次', '承包商／自维班',
                     '净差（人时）', '班次组号', '承包商组号', '签字']
RESIDUAL_COLUMNS = ['透视口径', '组号', '设备类型', '分类值', '工单数', '抽查前净差（人时）',
                    '本次扣减（人时）', '抽查后净差（人时）', '复核状态']
STUCK_COLUMNS = ['透视口径', '组号', '设备类型', '分类值', '净差（人时）',
                 '需扣减（人时）', '可扣减（人时）', '缺口（人时）']


def _parse_orders(rows):
    if not rows:
        raise ValueError('维保台账至少需要一张工单')
    orders = []
    seen = set()
    for index, row in enumerate(rows, start=1):
        try:
            order = WorkOrder.parse(row)
            if order.order_id in seen:
                raise ValueError(f'工单号 {order.order_id} 重复')
        except ValueError as error:
            raise ValueError(f'台账第 {index} 条 · {error}') from None
        seen.add(order.order_id)
        orders.append(order)
    return orders


def validate_rows(rows):
    _parse_orders(rows)


SIGNED_KEYS = ('signed_orders', 'signed_order_ids', 'signed_order_numbers',
               'signed_work_orders', 'signed_ids', 'signed')


def _parameters(params):
    if set(params) - {'dimension', 'review_hours', 'trace_group', *SIGNED_KEYS}:
        raise ValueError('存在未知工时透视参数')
    dimension = params.get('dimension', '班次')
    if not isinstance(dimension, str) or dimension.strip() not in DIMENSIONS:
        raise ValueError('交叉维度请选择班次或承包商')
    threshold = _number(params.get('review_hours', 12), '净超支复核线', CENT)
    trace_group = _text(params.get('trace_group'), '追溯组号', 8, required=False)
    signed = next((params[key] for key in SIGNED_KEYS if key in params), '')
    return dimension.strip(), threshold, trace_group, signed


def _category_sort(key):
    return tuple((value is None, value or '') for value in key)


def _pivot(orders, dimension):
    """Group totals and members for one asset-type cross dimension, with stable ids."""
    totals = defaultdict(Totals)
    members = defaultdict(list)
    for order in orders:
        key = (order.asset_type, getattr(order, DIMENSIONS[dimension]))
        totals[key].add(order.totals())
        members[key].append(order)
    keys = sorted(totals, key=_category_sort)
    ids = {key: f'G{index:04}' for index, key in enumerate(keys, start=1)}
    return ids, totals, members


def _signed_orders(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r'[,，、;；\s]+', raw)
    elif isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        raise ValueError('签字工单号必须是文本或清单')
    return [part.strip() for part in parts if part and part.strip()]


def _required_reduction(variance, threshold):
    """Smallest deduction that takes a flagged gross variance below the review line."""
    return variance if threshold == 0 else variance - threshold + 1


def _minimal_checklist(flagged, candidates, effort=600_000):
    """Fewest unsigned net-overrun orders that pull every flagged group below the line.

    Each order touches at most one shift group and one contractor group, so the
    candidates form edges over a small node set. The exact branch-and-bound
    returns the minimum-count selection with the lexicographically earliest
    order list on ties. Very large instances fall back to a deterministic
    greedy cover so a run never blocks; ``exact`` reports which path was used.
    """
    needed = tuple(node['required'] for node in flagged)
    weights = [candidate['weight'] for candidate in candidates]
    touched = [candidate['nodes'] for candidate in candidates]
    total = len(candidates)
    budget = {'left': effort}

    def greedy(score):
        remaining = list(needed)
        chosen = []
        used = set()
        while any(demand > 0 for demand in remaining):
            best, best_score = None, None
            for position in range(total):
                if position in used:
                    continue
                gain = sum(min(remaining[index], weights[position])
                           for index in touched[position] if remaining[index] > 0)
                if gain <= 0:
                    continue
                key = score(gain, weights[position])
                if best_score is None or key > best_score:
                    best, best_score = position, key
            if best is None:
                return None
            used.add(best)
            chosen.append(best)
            for index in touched[best]:
                remaining[index] -= weights[best]
        return chosen

    def best_greedy():
        strategies = (lambda gain, weight: gain,
                      lambda gain, weight: weight,
                      lambda gain, weight: (gain, weight))
        covers = [cover for cover in (greedy(strategy) for strategy in strategies) if cover is not None]
        return min(covers, key=lambda cover: (len(cover), cover)) if covers else None

    def lower_bound(start, remaining):
        best = 0
        for index, demand in enumerate(remaining):
            if demand <= 0:
                continue
            available = sorted((weights[pos] for pos in range(start, total)
                                if index in touched[pos]), reverse=True)
            running = used = 0
            for weight in available:
                running += weight
                used += 1
                if running >= demand:
                    break
            if running < demand:
                return None
            best = max(best, used)
        return best

    def search(start, slots, remaining, misses):
        if all(demand <= 0 for demand in remaining):
            return []
        if slots <= 0:
            return None
        budget['left'] -= 1
        if budget['left'] < 0:
            return None
        bound = lower_bound(start, remaining)
        if bound is None or bound > slots:
            return None
        key = (start, slots, remaining)
        if key in misses:
            return None
        for position in range(start, total):
            gains = [index for index in touched[position] if remaining[index] > 0]
            if not gains:
                continue
            updated = list(remaining)
            for index in gains:
                updated[index] -= weights[position]
            tail = search(position + 1, slots - 1, tuple(updated), misses)
            if tail is not None:
                return [position, *tail]
        misses.add(key)
        return None

    if not needed:
        return [], True
    bound = lower_bound(0, needed)
    if bound is None:
        return None, True
    fallback = best_greedy()
    if fallback is None:
        # Taking every candidate always covers each group's full net overshoot,
        # so a feasible instance is never reported as unsolvable.
        fallback = list(range(total))
    ceiling = len(fallback)
    for limit in range(bound, ceiling + 1):
        found = search(0, limit, needed, set())
        if found is not None:
            return [candidates[position]['order_id'] for position in found], True
        if budget['left'] < 0:
            break
    return [candidates[position]['order_id'] for position in fallback], len(fallback) <= bound


def _checklist(orders, threshold, signed, pivots):
    """Dual asset-type pivots: minimal signed spot-check list and residual variances."""
    signed = set(signed)
    known = {order.order_id for order in orders}
    unknown = sorted(signed - known)
    nodes = {}
    flagged = []
    for dimension in DIMENSIONS:
        ids, totals, members = pivots[dimension]
        for key in sorted(totals, key=_category_sort):
            totals_key = totals[key]
            if totals_key.variance > 0 and totals_key.variance >= threshold:
                node = {
                    'dimension': dimension, 'key': key, 'group': ids[key],
                    'variance': totals_key.variance, 'count': totals_key.count,
                    'required': _required_reduction(totals_key.variance, threshold),
                    'available': sum(order.totals().variance for order in members[key]
                                     if order.totals().variance > 0 and order.order_id not in signed),
                }
                flagged.append(node)
                nodes[(dimension, key)] = len(flagged) - 1

    candidates = []
    for order in orders:
        variance = order.totals().variance
        if variance <= 0 or order.order_id in signed:
            continue
        touches = []
        for dimension in DIMENSIONS:
            node = nodes.get((dimension, (order.asset_type, getattr(order, DIMENSIONS[dimension]))))
            if node is not None:
                touches.append(node)
        if touches:
            candidates.append({'order_id': order.order_id, 'weight': variance,
                               'nodes': tuple(sorted(touches))})
    candidates.sort(key=lambda item: item['order_id'])
    stuck = [node for node in flagged if node['available'] < node['required']]
    selection, exact = (None, True) if stuck else _minimal_checklist(flagged, candidates)
    chosen = set(selection) if selection is not None else set()
    return {'nodes': flagged, 'selection': selection, 'chosen': chosen, 'exact': exact,
            'stuck': stuck, 'unknown': unknown, 'candidate_count': len(candidates)}


def _metric(label, value, detail):
    return {'label': label, 'value': value, 'detail': detail}


def _table(title, columns, rows):
    return {'title': title, 'columns': columns, 'rows': rows}


def analyze(rows, params):
    dimension, threshold, trace_group, signed = _parameters(params)
    orders = _parse_orders(rows)
    signed_ids = _signed_orders(signed)
    pivots = {name: _pivot(orders, name) for name in DIMENSIONS}
    ids, group_totals, group_orders = pivots[dimension]
    coverage = {(False, False): Totals(), (True, False): Totals(),
                (False, True): Totals(), (True, True): Totals()}
    ledger = Totals()
    for order in orders:
        key = (order.asset_type, getattr(order, DIMENSIONS[dimension]))
        totals = order.totals()
        coverage[key[0] is None, key[1] is None].add(totals)
        ledger.add(totals)

    groups = [(ids[key], key, group_totals[key]) for key in sorted(group_totals, key=_category_sort)]
    if trace_group and all(group_id != trace_group for group_id, _, _ in groups):
        raise ValueError('找不到追溯组号，请从当前批次和交叉维度的透视表复制组号')
    groups.sort(key=lambda group: (-group[2].variance, group[0]))

    group_sum = Totals()
    trace_sum = Totals()
    trace_rows = []
    pivot_rows = []
    flagged = 0
    for group_id, key, totals in groups:
        review = totals.variance > 0 and totals.variance >= threshold
        flagged += review
        group_sum.add(totals)
        pivot_rows.append([
            group_id, *map(_category, key), totals.count, *totals.hours_row(), totals.percent,
            '待复核' if review else '未触线',
        ])
        if trace_group is None or trace_group == group_id:
            for order in sorted(group_orders[key], key=lambda item: (item.work_date, item.order_id)):
                line_totals = order.totals()
                trace_rows.append(order.detail(group_id, line_totals))
                trace_sum.add(line_totals)

    other_dimension = next(name for name in DIMENSIONS if name != dimension)
    other_ids, other_totals, _ = pivots[other_dimension]
    other_pivot_rows = [
        [other_ids[key], *map(_category, key), totals.count, *totals.hours_row(), totals.percent,
         '待复核' if totals.variance > 0 and totals.variance >= threshold else '未触线']
        for key, totals in sorted(other_totals.items(), key=lambda kv: (-kv[1].variance, other_ids[kv[0]]))
    ]

    coverage_labels = ['分类齐全', '仅缺设备类型', f'仅缺{dimension}', f'同时缺设备类型与{dimension}']
    coverage_sum = Totals()
    missing = Totals()
    coverage_rows = []
    for (key, totals), label in zip(coverage.items(), coverage_labels, strict=True):
        coverage_rows.append([label, totals.count, *totals.hours_row()])
        coverage_sum.add(totals)
        if any(key):
            missing.add(totals)

    group_gap = group_sum.difference(ledger)
    coverage_gap = coverage_sum.difference(ledger)
    trace_target = next((totals for group_id, _, totals in groups if group_id == trace_group), ledger)
    trace_gap = trace_sum.difference(trace_target)
    reconciled = group_gap == coverage_gap == trace_gap == Totals()
    driver_gap = ledger.variance - ledger.volume - ledger.efficiency - ledger.unbudgeted

    checklist = _checklist(orders, threshold, signed_ids, pivots)
    order_index = {order.order_id: order for order in orders}
    selected = sorted(checklist['chosen'])
    checklist_rows = []
    for position, order_id in enumerate(selected, start=1):
        order = order_index[order_id]
        checklist_rows.append([
            position, order.order_id, order.work_date, order.asset_id,
            _category(order.asset_type), _category(order.shift), _category(order.contractor),
            _hours(order.totals().variance),
            pivots['班次'][0][(order.asset_type, order.shift)],
            pivots['承包商'][0][(order.asset_type, order.contractor)],
            '待签字' if checklist['selection'] is not None else '未定',
        ])

    residual_rows = []
    for name in DIMENSIONS:
        ids_dim, totals_dim, _ = pivots[name]
        deducted = defaultdict(int)
        for order_id in selected:
            order = order_index[order_id]
            deducted[(order.asset_type, getattr(order, DIMENSIONS[name]))] += order.totals().variance
        ordered = sorted(totals_dim, key=lambda key: (-totals_dim[key].variance, ids_dim[key]))
        for key in ordered:
            cut = deducted.get(key, 0)
            before = totals_dim[key].variance
            if cut == 0 and not (before > 0 and before >= threshold):
                continue
            after = before - cut
            triggered = after > 0 and after >= threshold
            residual_rows.append([
                name, ids_dim[key], *map(_category, key), totals_dim[key].count,
                _hours(before), _hours(cut), _hours(after), '待复核' if triggered else '未触线',
            ])

    stuck_rows = []
    for node in checklist['stuck']:
        deficit = node['required'] - node['available']
        stuck_rows.append([
            node['dimension'], node['group'], *map(_category, node['key']), _hours(node['variance']),
            _hours(node['required']), _hours(node['available']), _hours(max(deficit, 0)),
        ])
    solved = checklist['selection'] is not None

    summary = f'本批 {ledger.count} 张工单，实耗 {_hours(ledger.actual):.2f} 人时，净差 {_hours(ledger.variance):+.2f} 人时。'
    if flagged:
        group_id, key, largest = groups[0]
        label = '／'.join(map(_category, key))
        summary += f'优先复核 {group_id} {label}（净超 {_hours(largest.variance):.2f} 人时），共 {flagged} 组触线。'
    else:
        summary += f'按 {_hours(threshold):.2f} 人时复核线，本批无触线组合。'
    if not flagged:
        summary += '两种透视均无需抽查。'
    elif solved and not selected:
        summary += '现有待复核组已满足复核线，无需抽查。'
    elif solved:
        summary += f'两种透视合计需抽查 {len(selected)} 张净超支且未签字的工单，抽查后待复核组全部降到复核线以下。'
        if not checklist['exact']:
            summary += '（组合规模较大，已用启发式取可行清单，张数可能非最少。）'
    else:
        groups_text = '、'.join(f'{node["dimension"]}{node["group"]}' for node in checklist['stuck'])
        summary += f'抽查清单无解，卡住的组：{groups_text}。'
    if checklist['unknown']:
        summary += f'签字工单号中 {len(checklist["unknown"])} 个不在本批底账：{"、".join(checklist["unknown"])}。'
    if missing.count:
        summary += f'台账组需补分类 {missing.count} 张，已纳入 {_hours(missing.actual):.2f} 人时实耗。'

    chart_groups = sorted(groups, key=lambda group: (-abs(group[2].variance), group[0]))[:12]
    reconciliation = [
        ('工单底账', ledger), ('透视分组合计', group_sum), ('分类分桶合计', coverage_sum),
        ('分组减底账', group_gap), ('分类分桶减底账', coverage_gap),
        ('当前追溯合计', trace_sum), ('追溯合计减对应范围', trace_gap),
    ]
    trace_title = f'工单追溯 · {trace_group or "全量"}'
    after_flagged = sum(1 for row in residual_rows if row[-1] == '待复核')
    return {
        'summary': summary,
        'metrics': [
            _metric('预算工时（人时）', _hours(ledger.budget), f'{ledger.count} 张工单'),
            _metric('实耗工时（人时）', _hours(ledger.actual), f'净差 {_hours(ledger.variance):+.2f} 人时'),
            _metric('加权预算偏差率（%）', ledger.percent, '净差除以预算总额，零预算时留空'),
            _metric('净超支待复核组', flagged, f'净超支为正且达到 {_hours(threshold):.2f} 人时'),
            _metric('抽查清单（张）', len(selected) if solved else '无解',
                    f'净超支且未签字，两种透视并集；抽查后剩余待复核组 {after_flagged}'),
            _metric('含缺失分类工单', missing.count, f'按设备类型与{dimension}检查，实耗 {_hours(missing.actual):.2f} 人时'),
            _metric('对账状态', '一致' if reconciled and driver_gap == 0 else '有差额',
                    f'分组、分类与追溯均逐项对账，驱动项剩余 {_hours(driver_gap):.2f} 人时'),
        ],
        'tables': [
            _table(f'设备类型 × {dimension}工时透视',
                   ['组号', '设备类型', dimension, '工单数', *HOUR_COLUMNS, '加权偏差率（%）', '复核状态'],
                   pivot_rows),
            _table(f'设备类型 × {other_dimension}工时透视',
                   ['组号', '设备类型', other_dimension, '工单数', *HOUR_COLUMNS, '加权偏差率（%）', '复核状态'],
                   other_pivot_rows),
            _table('抽查清单 · 净超支且未签字',
                   CHECKLIST_COLUMNS, checklist_rows),
            _table('抽查后两边净差 · 班次与承包商', RESIDUAL_COLUMNS, residual_rows),
            *([_table('抽查无解 · 卡住的组', STUCK_COLUMNS, stuck_rows)] if stuck_rows else []),
            _table('分类覆盖与缺失分类', ['分类状态', '工单数', *HOUR_COLUMNS], coverage_rows),
            _table('工时总额与追溯范围对账', ['对账项', '工单数', *HOUR_COLUMNS],
                   [[label, totals.count, *totals.hours_row()] for label, totals in reconciliation]),
            _table(trace_title, ['组号', '工单号', '作业日期', '资产编号', '设备类型', '班次', '承包商／自维班',
                                 '计划作业点数', '完成作业点数', *HOUR_COLUMNS], trace_rows),
        ],
        'notes': [
            '抽查清单同时覆盖设备类型×班次与设备类型×承包商两种透视；每张工单的净差从所属两组各扣一次。',
            '只选净超支且未签字的工单，取张数最少的方案；同数时取工单号排序最靠前的一案。',
            '抽查后原先待复核的组全部降到复核线以下，表格保留抽查前后两边净差。',
            '原预算单耗乘作业点数增减得到作业量影响，按工单四舍五入到百分之一人时。单耗及其他为差额，需结合签工记录判断。',
            f'{trace_title}含 {trace_sum.count} 张。可用组号重跑缩小明细范围，透视与分类对账仍覆盖全批。',
        ],
        'series_title': '按净差绝对值排列的前 12 组（人时）',
        'series': [{'label': f'{group_id} {"／".join(map(_category, key))}', 'value': _hours(totals.variance)}
                   for group_id, key, totals in chart_groups],
        'ready': reconciled and driver_gap == 0,
    }
