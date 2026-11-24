from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from itertools import combinations, product
from math import comb, lcm, prod
from random import Random


TIME_FORMAT = "%Y-%m-%d %H:%M"
SLOT_HOURS = 2
SLOTS_PER_BLOCK = 6
BLOCK_HOURS = SLOT_HOURS * SLOTS_PER_BLOCK
ORDERS = ("AB", "BA")
EXACT_LIMIT = 50_000
MONTE_CARLO_DRAWS = 20_000
DEFAULT_SEED = 691124


@dataclass(frozen=True)
class Plan:
    start: datetime
    pair_count: int

    def pair_start(self, number: int) -> datetime:
        return self.start + timedelta(hours=2 * BLOCK_HOURS * (number - 1))


@dataclass(frozen=True)
class Window:
    eligible: int
    on_time: int


def sample_rows() -> list[dict]:
    start = datetime(2026, 11, 9, 8)
    schedules = {
        "海陵港东岸运营区": ("AB", "BA", "BA", "AB", "BA", "AB"),
        "海陵港南岸运营区": ("BA", "AB", "BA", "AB", "AB", "BA"),
    }
    rows = []
    for zone_index, (zone, orders) in enumerate(schedules.items()):
        plan = Plan(start, len(orders))
        for pair, order in enumerate(orders, 1):
            for block, guide in enumerate(order, 1):
                block_start = plan.pair_start(pair) + timedelta(hours=BLOCK_HOURS * (block - 1))
                for slot in range(SLOTS_PER_BLOCK):
                    eligible = 2 + (pair + slot + zone_index + block) % 2
                    on_time = eligible - 1
                    if slot == 0 and guide == "B":
                        on_time -= 1
                    if slot in (2, 4) and guide == "B":
                        on_time += 1
                    rows.append({
                        "zone": zone,
                        "planned_zones": len(schedules),
                        "plan_start": start.strftime(TIME_FORMAT),
                        "planned_pairs": len(orders),
                        "pair": pair,
                        "block": block,
                        "window_start": (block_start + timedelta(hours=SLOT_HOURS * slot)).strftime(TIME_FORMAT),
                        "sequence": order,
                        "guide": guide,
                        "eligible": eligible,
                        "on_time": on_time,
                    })
    return rows


def _parse_time(value: str, label: str) -> datetime:
    try:
        timestamp = datetime.strptime(value, TIME_FORMAT)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{label}格式无效，请按北京时间 YYYY-MM-DD HH:MM 补录。") from error
    if timestamp.strftime(TIME_FORMAT) != value:
        raise ValueError(f"{label}格式无效，请按北京时间 YYYY-MM-DD HH:MM 补录。")
    return timestamp


def _read_ledger(rows: list[dict]) -> tuple[dict, dict, dict]:
    plans = {}
    orders = {}
    windows = {}
    zone_counts = set()
    for index, row in enumerate(rows, 2):
        zone = row["zone"].strip()
        label = f"CSV 第 {index} 行（{zone}）"
        if not zone or row["planned_zones"] < 1:
            raise ValueError(f"{label}缺少有效的作业区或计划作业区数，请按冻结排班补齐。")
        zone_counts.add(row["planned_zones"])
        pair_count = row["planned_pairs"]
        if pair_count < 2 or pair_count % 2:
            raise ValueError(f"{label}计划配对数须为至少 2 的偶数，请核对 AB、BA 各半的冻结排班。")
        start = _parse_time(row["plan_start"], f"{label}的计划起点")
        try:
            start + timedelta(hours=2 * BLOCK_HOURS * pair_count)
        except OverflowError as error:
            raise ValueError(f"{label}计划跨度超出可用日期，请核对计划起点和计划配对数。") from error
        if start.minute or start.hour not in (8, 20):
            raise ValueError(f"{label}计划起点须对齐 08:00 或 20:00 交班，请核对排班时钟。")
        plan = Plan(start, pair_count)
        if plans.setdefault(zone, plan) != plan:
            raise ValueError(f"{label}同一作业区的计划起点和配对总数不一致，请使用同一版冻结排班。")
        pair, block = row["pair"], row["block"]
        if not 1 <= pair <= pair_count or block not in (1, 2):
            raise ValueError(f"{label}配对号或班次号超出排班，请补录计划内配对号及班次 1 或 2。")
        order = row["sequence"]
        if order not in ORDERS or orders.setdefault((zone, pair), order) != order:
            raise ValueError(f"{label}配对的分配顺序冲突，请核对随机排班中的 AB/BA。")
        if row["guide"] != order[block - 1]:
            raise ValueError(f"{label}发布指引与分配顺序不符，请核对发布日志与冻结排班后重新导入。")
        window_start = _parse_time(row["window_start"], f"{label}的窗口起点")
        block_start = plan.pair_start(pair) + timedelta(hours=BLOCK_HOURS * (block - 1))
        slot, remainder = divmod(window_start - block_start, timedelta(hours=SLOT_HOURS))
        if remainder or not 0 <= slot < SLOTS_PER_BLOCK:
            raise ValueError(f"{label}窗口越界或未对齐 2 小时网格，请按该班次起点重填。")
        key = (zone, pair, block, slot)
        if key in windows:
            raise ValueError(f"{label}重复窗口，请按作业区、配对、班次和窗口起点去重。")
        eligible, on_time = row["eligible"], row["on_time"]
        if not 0 <= on_time <= eligible:
            raise ValueError(f"{label}工单数无效，请填报非负分母，且达标数不得超过计划工单数。")
        windows[key] = Window(eligible, on_time)
    if zone_counts != {len(plans)}:
        raise ValueError("作业区数量与冻结计划不符，请补齐缺报作业区或更正计划作业区数。")

    assigned = defaultdict(list)
    for (zone, _), order in orders.items():
        assigned[zone].append(order)
    for zone, plan in plans.items():
        if any(assigned[zone].count(order) > plan.pair_count // 2 for order in ORDERS):
            raise ValueError(f"{zone}的 AB/BA 数量超出各半配额，请按随机排班复查分配顺序。")
    return plans, orders, windows


def _option_number(options: dict, key: str, default: int | float, minimum: int | float,
                   maximum: int | float, label: str) -> Fraction:
    try:
        value = Fraction(str(options.get(key, default)))
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError(f"{label}无效，请填写 {minimum} 至 {maximum} 之间的数值。") from error
    if not Fraction(str(minimum)) <= value <= Fraction(str(maximum)):
        raise ValueError(f"{label}超出范围，请填写 {minimum} 至 {maximum} 之间的数值。")
    return value


def _percent(value: Fraction, digits: int = 2) -> float:
    return round(float(value * 100), digits)


def _randomization(
    strata: list[list[tuple[str, Fraction]]], space: int, seed: int,
) -> tuple[Fraction, int, int, str]:
    """Compare selected AB sums on an exact integer scale, including ties."""
    scale = lcm(*(gap.denominator for stratum in strata for _, gap in stratum))
    scores = [[gap.numerator * (scale // gap.denominator) for _, gap in stratum] for stratum in strata]
    observed = sum(score for stratum, values in zip(strata, scores)
                   for (order, _), score in zip(stratum, values) if order == "AB")
    if space <= EXACT_LIMIT:
        possible = [
            [sum(subset) for subset in combinations(values, len(values) // 2)]
            for values in scores
        ]
        hits = sum(sum(totals) >= observed for totals in product(*possible))
        return Fraction(hits, space), space, hits, "精确枚举"

    rng = Random(seed)
    hits = 0
    for _ in range(MONTE_CARLO_DRAWS):
        selected = sum(sum(rng.sample(values, len(values) // 2)) for values in scores)
        hits += selected >= observed
    return Fraction(hits + 1, MONTE_CARLO_DRAWS + 1), MONTE_CARLO_DRAWS, hits, "蒙特卡洛抽样"


def compute(rows: list[dict], options: dict) -> dict:
    if not rows:
        raise ValueError("未收到窗口台账，请导入至少一个包含 AB/BA 排班与工单统计的 CSV。")
    washout_raw = str(options.get("washout_hours", "2"))
    if washout_raw not in ("2", "4"):
        raise ValueError("洗脱时长须为 2 或 4 小时，请选择与交班记录一致的完整窗口。")
    washout = int(washout_raw)
    slot_range = range(washout // SLOT_HOURS, SLOTS_PER_BLOCK)
    coverage_limit = _option_number(options, "coverage_pct", 90, 50, 100, "最低覆盖率")
    minimum_gain = _option_number(options, "min_gain_pp", 8, 0, 100, "最低业务提升")
    alpha = _option_number(options, "alpha", 0.05, 0.01, 0.20, "显著性门槛")
    min_calls = _option_number(options, "min_calls", 5, 1, 100, "单班最少计划工单")
    seed = _option_number(options, "seed", DEFAULT_SEED, 0, 2**31 - 1, "随机种子")
    if min_calls.denominator != 1 or seed.denominator != 1:
        raise ValueError("单班最少计划工单和随机种子须为整数，请去掉小数后重试。")
    plans, orders, windows = _read_ledger(rows)

    details = []
    strata = []
    rates = {"A": [], "B": []}
    included_orders = {"AB": 0, "BA": 0}
    order_gains = {"AB": [], "BA": []}
    available_slots = 0
    total_pairs = sum(plan.pair_count for plan in plans.values())
    slots_per_pair = 2 * len(slot_range)
    zone_coverage = {}
    for zone, plan in sorted(plans.items()):
        stratum = []
        zone_slots = zone_used = 0
        for pair in range(1, plan.pair_count + 1):
            order = orders.get((zone, pair), "")
            blocks = [
                [windows[(zone, pair, block, slot)] for slot in slot_range
                 if (zone, pair, block, slot) in windows]
                for block in (1, 2)
            ]
            seen = sum(map(len, blocks))
            zone_slots += seen
            denominators = [sum(window.eligible for window in block) for block in blocks]
            numerators = [sum(window.on_time for window in block) for block in blocks]
            gap = Fraction(0)
            rate_a = rate_b = gain = "—"
            if seen != slots_per_pair:
                status = "缺少洗脱后窗口" if order else "整对缺报"
            elif min(denominators) < min_calls:
                status = "单班计划工单低于门槛"
            else:
                first, second = [Fraction(n, d) for n, d in zip(numerators, denominators)]
                a, b = (first, second) if order == "AB" else (second, first)
                gap = second - first
                effect = b - a
                rates["A"].append(a)
                rates["B"].append(b)
                included_orders[order] += 1
                order_gains[order].append(effect)
                rate_a, rate_b, gain = _percent(a), _percent(b), _percent(effect)
                zone_used += 1
                status = "纳入"
            stratum.append((order, gap))
            start = plan.pair_start(pair)
            details.append({
                "作业区": zone,
                "配对号": pair,
                "第1班起点": start.strftime(TIME_FORMAT),
                "第2班起点": (start + timedelta(hours=BLOCK_HOURS)).strftime(TIME_FORMAT),
                "分配顺序": order or "待补报",
                "保留窗口": f"{seen}/{slots_per_pair}",
                "第1班达标/计划": f"{numerators[0]}/{denominators[0]}" if blocks[0] else "—",
                "第2班达标/计划": f"{numerators[1]}/{denominators[1]}" if blocks[1] else "—",
                "A达标率%": rate_a,
                "B达标率%": rate_b,
                "B−A百分点": gain,
                "状态": status,
            })
        available_slots += zone_slots
        zone_coverage[zone] = (Fraction(zone_slots, plan.pair_count * slots_per_pair),
                               Fraction(zone_used, plan.pair_count))
        strata.append(stratum)

    included = len(rates["A"])
    window_coverage = Fraction(available_slots, total_pairs * slots_per_pair)
    pair_coverage = Fraction(included, total_pairs)
    space = prod(comb(plan.pair_count, plan.pair_count // 2) for plan in plans.values())
    metrics = {
        "计划配对数": total_pairs,
        "纳入配对数": included,
        "纳入AB配对数": included_orders["AB"],
        "纳入BA配对数": included_orders["BA"],
        "洗脱时长（小时）": washout,
        "计划保留窗口数": total_pairs * slots_per_pair,
        "实收保留窗口数": available_slots,
        "窗口覆盖率%": _percent(window_coverage),
        "配对覆盖率%": _percent(pair_coverage),
        "最低覆盖率%": float(coverage_limit),
        "最低业务提升（百分点）": float(minimum_gain),
        "显著性门槛": float(alpha),
        "A等权达标率%": "—",
        "B等权达标率%": "—",
        "B−A配对提升（百分点）": "—",
        "单侧随机化p值": "—",
        "随机化方法": "待计算",
        "分配空间规模": str(space),
        "实际评估分配数": 0,
        "尾部命中数": 0,
    }
    notes = [
        "各作业区独立等概率抽签，固定 AB、BA 各半。每对按 B 班达标率减 A 班达标率计算，纳入配对等权。",
        "每个班次起点统一执行洗脱，包括相邻班指引相同的情形。任一洗脱后窗口缺报或任一班低于工单门槛，整对退出效果计算。",
        "随机化保留各作业区全部计划配对及配额。退出配对的得分固定为零，顺序仍参与分配。单侧 p 值检验指引分配对任何窗口均无影响的尖锐零假设。",
        "工单分母须在抽签前冻结，缺报须与指引分配无关。应核对两区作业班组互不串班，且交班残留影响短于所选洗脱时长。",
        "试点放行要求覆盖与顺序均达标、单侧检验达标、实测提升达到业务门槛。p 值对应零效应检验，收益门槛按点估计复核。",
    ]
    low_coverage = []
    for zone, (zone_windows, zone_pairs) in zone_coverage.items():
        notes.append(f"{zone}窗口覆盖 {_percent(zone_windows):g}%，配对覆盖 {_percent(zone_pairs):g}%。")
        if min(zone_windows, zone_pairs) * 100 < coverage_limit:
            low_coverage.append(zone)

    points = []
    p_value = None
    gain_pp = None
    if included:
        means = {guide: sum(values, Fraction(0)) / included for guide, values in rates.items()}
        gain_pp = (means["B"] - means["A"]) * 100
        metrics.update({
            "A等权达标率%": _percent(means["A"]),
            "B等权达标率%": _percent(means["B"]),
            "B−A配对提升（百分点）": round(float(gain_pp), 2),
        })
        points = [{"label": order, "value": _percent(sum(values, Fraction(0)) / len(values))}
                  for order, values in order_gains.items() if values]
    both_orders = all(included_orders.values())
    if both_orders:
        p_value, evaluated, hits, method = _randomization(strata, space, int(seed))
        metrics.update({
            "单侧随机化p值": float(p_value),
            "随机化方法": method,
            "实际评估分配数": evaluated,
            "尾部命中数": hits,
        })
        if method == "蒙特卡洛抽样":
            metrics["抽样种子"] = int(seed)
            notes.append(f"固定种子 {seed} 独立等概率抽样 {evaluated} 次，p 值为（尾部命中数＋1）/（抽样次数＋1）。")

    can_publish = not low_coverage and both_orders
    if not can_publish:
        reasons = []
        if low_coverage:
            reasons.append(f"{'、'.join(low_coverage)}覆盖不足，请补齐窗口或扩展随机排班")
        if not both_orders:
            reasons.append("纳入配对须同时覆盖 AB 和 BA，请补齐另一顺序的完整配对")
        headline = "暂缓发布。" + "。".join(reasons) + "。"
    elif gain_pp <= 0:
        headline = "保留 A。B 未带来正向的限时通电提升。"
    elif gain_pp < minimum_gain:
        headline = "保留 A。B 的实测提升未达到试点收益门槛。"
    elif p_value > alpha:
        headline = "延长试验。提升达到业务门槛，随机化证据仍不足。"
    else:
        headline = f"建议分区上线 B。限时通电达标率提升 {float(gain_pp):.2f} 个百分点。"
    return {
        "headline": headline,
        "metrics": metrics,
        "rows": details,
        "notes": notes,
        "chart": {"label": "按分配顺序查看限时通电提升", "unit": "百分点", "points": points},
        "can_publish": can_publish,
    }
