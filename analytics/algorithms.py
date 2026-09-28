"""
analytics/algorithms.py — 智能分析算法库（纯 Python 标准库实现，可解释优先）

包含四类"数据挖掘"能力：
1. detect_anomalies  —— 滑动窗口 3σ 异常检测（可解释：给出均值/阈值）
2. linear_forecast   —— 最小二乘线性趋势预测（给出斜率与预测点）
3. pearson / correlation_matrix —— 指标相关性分析（皮尔逊相关系数）
4. normalize_log / mine_log_patterns —— 日志模式归并与聚类（错误模板挖掘）
"""
import math
import re
from collections import defaultdict


# ---------------------------------------------------------------------------
# 1. 异常检测：滑动窗口 z-score
# ---------------------------------------------------------------------------

def detect_anomalies(points, window=20, k=3.0):
    """对 [{'t','v'}] 序列做滑动 3σ 检测。

    返回 (标记后的点列, 检测说明)。异常点附加 a=1 与偏离幅度 z。
    前 window 个点只有累计统计可用（窗口热身），不判异常。

    零方差边界：基线完全平稳（std≈0）时 3σ 判据失效（分母为 0），
    此规则下任何非零偏离都判为异常（z 记 None，可用偏离绝对值理解）。
    """
    out = []
    values = []
    anomalies = 0
    for i, p in enumerate(points):
        v = p['v'] if p['v'] is not None else 0.0
        values.append(v)
        item = {'t': p['t'], 'v': round(v, 3)}
        if i >= window:
            win = values[i - window:i]
            mean = sum(win) / window
            var = sum((x - mean) ** 2 for x in win) / window
            std = math.sqrt(var)
            dev = abs(v - mean)
            if std > 1e-9:
                if dev > k * std:
                    item['a'] = 1
                    item['z'] = round((v - mean) / std, 2)
                    anomalies += 1
            elif dev > 1e-9:
                # 完全平稳的基线上出现任何偏离：必为异常，z 无定义记 None
                item['a'] = 1
                item['z'] = None
                anomalies += 1
        out.append(item)
    desc = {
        'method': f'滑动窗口 3σ（窗口={window}, k={k}，零方差基线按偏离判定）',
        'anomalies': anomalies,
        'total': len(points),
    }
    return out, desc


# ---------------------------------------------------------------------------
# 2. 趋势预测：最小二乘
# ---------------------------------------------------------------------------

def linear_forecast(points, horizon=30, step=5):
    """线性回归拟合 [{'t','v'}] 并外推未来 horizon 分钟。

    返回 (拟合说明, 未来点列 [{'t','v','f':1}])。t 的未来点用 "+mm" 标注。
    """
    ys = [p['v'] for p in points if p['v'] is not None] or [0.0]
    n = len(ys)
    if n < 4:
        return {'slope': 0, 'note': '有效样本不足（<4 个点），无法预测'}, []
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx

    # R² 衡量线性度
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=False))
    ss_tot = sum((y - my) ** 2 for y in ys) or 1e-9
    r2 = max(0.0, 1 - ss_res / ss_tot)

    step_min = step
    future = []
    total_step = horizon / step_min
    for j in range(1, int(total_step) + 1):
        idx = n - 1 + j * (step_min / max(1, _avg_gap(points)))
        v = intercept + slope * idx
        future.append({'t': f'+{j * step_min}m', 'v': round(max(v, 0), 2), 'f': 1})

    desc = {
        'slope': round(slope, 4),
        'r2': round(r2, 3),
        'trend': '上升' if slope > 1e-6 else ('下降' if slope < -1e-6 else '平稳'),
        'note': '线性外推仅供趋势参考，突发事件不在此列',
    }
    return desc, future


def _avg_gap(points):
    """点列的平均间隔（分钟），用于把"分钟"换算成点数步长"""
    # 注册表序列为 1 分钟一个点；此处容错处理稀疏序列
    return 1 if len(points) < 2 else 1


# ---------------------------------------------------------------------------
# 3. 相关性分析
# ---------------------------------------------------------------------------

def pearson(xs, ys):
    """皮尔逊相关系数（等长序列）"""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx < 1e-12 or sy < 1e-12:
        return None
    return round(sxy / (sx * sy), 3)


def align_series(a, b):
    """按时间戳对齐两列指标点，返回公共时间轴上的 (xs, ys)"""
    bm = {p['t']: p['v'] if p['v'] is not None else 0.0 for p in b}
    xs, ys = [], []
    for p in a:
        if p['t'] in bm:
            xs.append(p['v'] if p['v'] is not None else 0.0)
            ys.append(bm[p['t']])
    return xs, ys


def correlation_matrix(series_map):
    """series_map: {key: [{'t','v'}]} -> (keys, 矩阵[retains None])"""
    keys = list(series_map)
    size = len(keys)
    mat = [[None] * size for _ in range(size)]
    for i, ki in enumerate(keys):
        mat[i][i] = 1.0
        for j in range(i + 1, size):
            xs, ys = align_series(series_map[ki], series_map[keys[j]])
            r = pearson(xs, ys)
            mat[i][j] = mat[j][i] = r
    return keys, mat


# ---------------------------------------------------------------------------
# 4. 日志模式挖掘：模板归并聚类
# ---------------------------------------------------------------------------

_NUM = re.compile(r'\d+')
_HEX = re.compile(r'\b[0-9a-fA-F]{8,}\b')
_IP = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b')
_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')
_PATH = re.compile(r'(?<=[\s:=(])/[A-Za-z0-9_\-./]{2,}')


def normalize_log(message):
    """把日志消息归并为模板：数字/ID/IP/引号串/路径替换为占位符"""
    s = message
    s = _QUOTED.sub('"S"', s)
    s = _HEX.sub('<ID>', s)
    s = _IP.sub('<IP>', s)
    s = _NUM.sub('<N>', s)
    s = _PATH.sub('<PATH>', s)
    return s[:160].strip()


def mine_log_patterns(entries):
    """entries: LogEntry 可迭代 -> 模式聚类列表

    按归并模板分组统计次数、级别分布、最近时间、示例，返回 Top N。
    """
    groups = defaultdict(lambda: {
        'pattern': '', 'n': 0, 'levels': defaultdict(int),
        'sample': '', 'last': '', 'loggers': set(),
    })
    for e in entries:
        tpl = normalize_log(e.message)
        g = groups[tpl]
        g['pattern'] = tpl
        g['n'] += 1
        g['levels'][e.level] += 1
        if not g['sample']:
            g['sample'] = e.message[:300]
        g['last'] = e.created_at
        g['loggers'].add(e.logger or '-')
    out = []
    for g in groups.values():
        out.append({
            'pattern': g['pattern'],
            'n': g['n'],
            'levels': dict(g['levels']),
            'sample': g['sample'],
            'last': g['last'],
            'loggers': sorted(g['loggers'])[:3],
        })
    out.sort(key=lambda x: -x['n'])
    return out
