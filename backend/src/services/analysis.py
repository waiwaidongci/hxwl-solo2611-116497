"""Work-order variance, drivers and reconciliation for the maintenance ledger."""

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


def _parameters(params):
    if set(params) - {'dimension', 'review_hours', 'trace_group'}:
        raise ValueError('存在未知工时透视参数')
    dimension = params.get('dimension', '班次')
    if not isinstance(dimension, str) or dimension.strip() not in DIMENSIONS:
        raise ValueError('交叉维度请选择班次或承包商')
    threshold = _number(params.get('review_hours', 12), '净超支复核线', CENT)
    trace_group = _text(params.get('trace_group'), '追溯组号', 8, required=False)
    return dimension.strip(), threshold, trace_group


def _category_sort(key):
    return tuple((value is None, value or '') for value in key)


def _metric(label, value, detail):
    return {'label': label, 'value': value, 'detail': detail}


def _table(title, columns, rows):
    return {'title': title, 'columns': columns, 'rows': rows}


def analyze(rows, params):
    dimension, threshold, trace_group = _parameters(params)
    orders = _parse_orders(rows)
    group_totals = defaultdict(Totals)
    group_orders = defaultdict(list)
    coverage = {(False, False): Totals(), (True, False): Totals(),
                (False, True): Totals(), (True, True): Totals()}
    ledger = Totals()
    for order in orders:
        key = (order.asset_type, getattr(order, DIMENSIONS[dimension]))
        totals = order.totals()
        group_totals[key].add(totals)
        group_orders[key].append(order)
        coverage[key[0] is None, key[1] is None].add(totals)
        ledger.add(totals)

    groups = [
        (f'G{index:04}', key, group_totals[key])
        for index, key in enumerate(sorted(group_totals, key=_category_sort), start=1)
    ]
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

    summary = f'本批 {ledger.count} 张工单，实耗 {_hours(ledger.actual):.2f} 人时，净差 {_hours(ledger.variance):+.2f} 人时。'
    if flagged:
        group_id, key, largest = groups[0]
        label = '／'.join(map(_category, key))
        summary += f'优先复核 {group_id} {label}（净超 {_hours(largest.variance):.2f} 人时），共 {flagged} 组触线。'
    else:
        summary += f'按 {_hours(threshold):.2f} 人时复核线，本批无触线组合。'
    if missing.count:
        summary += f'台账组需补分类 {missing.count} 张，已纳入 {_hours(missing.actual):.2f} 人时实耗。'

    chart_groups = sorted(groups, key=lambda group: (-abs(group[2].variance), group[0]))[:12]
    reconciliation = [
        ('工单底账', ledger), ('透视分组合计', group_sum), ('分类分桶合计', coverage_sum),
        ('分组减底账', group_gap), ('分类分桶减底账', coverage_gap),
        ('当前追溯合计', trace_sum), ('追溯合计减对应范围', trace_gap),
    ]
    trace_title = f'工单追溯 · {trace_group or "全量"}'
    return {
        'summary': summary,
        'metrics': [
            _metric('预算工时（人时）', _hours(ledger.budget), f'{ledger.count} 张工单'),
            _metric('实耗工时（人时）', _hours(ledger.actual), f'净差 {_hours(ledger.variance):+.2f} 人时'),
            _metric('加权预算偏差率（%）', ledger.percent, '净差除以预算总额，零预算时留空'),
            _metric('净超支待复核组', flagged, f'净超支为正且达到 {_hours(threshold):.2f} 人时'),
            _metric('含缺失分类工单', missing.count, f'按设备类型与{dimension}检查，实耗 {_hours(missing.actual):.2f} 人时'),
            _metric('对账状态', '一致' if reconciled and driver_gap == 0 else '有差额',
                    f'分组、分类与追溯均逐项对账，驱动项剩余 {_hours(driver_gap):.2f} 人时'),
        ],
        'tables': [
            _table(f'设备类型 × {dimension}工时透视',
                   ['组号', '设备类型', dimension, '工单数', *HOUR_COLUMNS, '加权偏差率（%）', '复核状态'],
                   pivot_rows),
            _table('分类覆盖与缺失分类', ['分类状态', '工单数', *HOUR_COLUMNS], coverage_rows),
            _table('工时总额与追溯范围对账', ['对账项', '工单数', *HOUR_COLUMNS],
                   [[label, totals.count, *totals.hours_row()] for label, totals in reconciliation]),
            _table(trace_title, ['组号', '工单号', '作业日期', '资产编号', '设备类型', '班次', '承包商／自维班',
                                 '计划作业点数', '完成作业点数', *HOUR_COLUMNS], trace_rows),
        ],
        'notes': [
            '各组偏差率使用本组预算总额作分母，整体偏差率使用全批预算总额。零预算的偏差率留空。',
            '原预算单耗乘作业点数增减得到作业量影响，按工单四舍五入到百分之一人时。单耗及其他为差额，需结合签工记录判断。',
            f'{trace_title}含 {trace_sum.count} 张。可用组号重跑缩小明细范围，透视与分类对账仍覆盖全批。',
        ],
        'series_title': '按净差绝对值排列的前 12 组（人时）',
        'series': [{'label': f'{group_id} {"／".join(map(_category, key))}', 'value': _hours(totals.variance)}
                   for group_id, key, totals in chart_groups],
        'ready': reconciled and driver_gap == 0,
    }
