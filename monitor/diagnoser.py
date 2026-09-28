"""
monitor/diagnoser.py — 自动诊断引擎（本项目核心创新，纯自研规则引擎，无外部依赖）

设计思路：静态扫描 + 动态指标相结合。

【静态部分】用 Python 标准库 ast 解析视图源码，内置 3 条规则：
  R1 循环内数据库查询（N+1 嫌疑）      —— 严重级别：高
  R2 循环内访问关联对象属性（疑似漏用 select_related / prefetch_related）—— 中
  R3 无 limit 的 all() 调用（全表加载） —— 中

【动态部分】读取中间件采集的 RequestMetric：
  D1 平均耗时超过阈值的慢请求路径      —— 高
  D2 慢查询集中的路径                  —— 中

每条结论（Finding）包含：问题位置 / 严重级别 / 推断原因 / 修复建议代码片段，
最终渲染成网页报告，也可导出 Markdown（/diagnose/export/）。

规则刻意做得偏保守并在描述中注明"疑似"，允许误报——
毕业设计场景下，可解释性比准确率更重要。
"""
import ast
import time
from pathlib import Path

from django.conf import settings

# 视为"数据库查询"的方法名（QuerySet API 的常见入口）
QUERY_METHODS = {
    'get', 'filter', 'exclude', 'all', 'count', 'exists', 'first', 'last',
    'values', 'values_list', 'annotate', 'aggregate', 'in_bulk',
    'create', 'update', 'delete', 'get_or_create', 'update_or_create',
}
WRITE_METHODS = {'create', 'update', 'delete', 'save'}

# 明确不是外键链的内建/集合方法（R2 误报排除）
NON_MODEL_ATTRS = {
    'items', 'keys', 'values_list', 'append', 'pop', 'get', 'update', 'copy',
    'strip', 'split', 'replace', 'format', 'join', 'lower', 'upper', 'count',
}

RULE_META = {
    'R1': {'title': '循环内数据库查询（N+1 嫌疑）', 'severity': '高'},
    'R2': {'title': '循环内访问外键关联对象（疑似未预取）', 'severity': '中'},
    'R3': {'title': '无 limit 的 all() 全表加载', 'severity': '中'},
    'D1': {'title': '慢请求路径（动态指标）', 'severity': '高'},
    'D2': {'title': '慢查询集中路径（动态指标）', 'severity': '中'},
}

SEVERITY_ORDER = {'高': 0, '中': 1, '低': 2}


def _target_files():
    """返回要扫描的 (可读路径, 绝对路径) 列表"""
    base = Path(settings.BASE_DIR)
    files = []
    for app_dir in sorted(base.iterdir()):
        if not app_dir.is_dir():
            continue
        views = app_dir / 'views.py'
        if views.exists():
            files.append((f'{app_dir.name}/views.py', views))
    return files


# ------------------------------------------------------------------
# 静态分析工具函数
# ------------------------------------------------------------------

def _is_queryset_call(node):
    """判断 Call 是否疑似一次数据库查询：
    形如 xxx.objects.method(...) 或 query[method](...)"""
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in QUERY_METHODS:
        return False
    value = node.func.value
    # Model.objects.xx / Model.objects.filter().xx() / qs.xx()
    if isinstance(value, ast.Attribute) and value.attr == 'objects':
        return True
    if isinstance(value, ast.Call):
        return True  # 链式调用，视为查询
    return False


def _fk_chain_root(node):
    """若节点是形如 a.b.c 的属性链（根为 Name），返回根名，否则 None"""
    depth = 0
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
        depth += 1
    if depth >= 2 and isinstance(cur, ast.Name):
        return cur.id
    return None


def _call_snippet(node, source):
    """取出该 Call 节点对应的源码片段（截断）"""
    try:
        seg = ast.get_source_segment(source, node) or ''
    except Exception:
        seg = ''
    return seg.strip()[:120]


class ViewAnalyzer(ast.NodeVisitor):
    """对单个视图函数做 3 条静态规则扫描"""

    def __init__(self, rel_path, source):
        self.rel_path = rel_path
        self.source = source
        self.lines = source.splitlines()
        self.findings = []
        self._seen = set()  # (rule, line) 去重：同一行同一规则只报告一次

    def _add(self, rule, lineno, evidence, cause, fix):
        key = (rule, lineno)
        if key in self._seen:
            return
        self._seen.add(key)
        meta = RULE_META[rule]
        self.findings.append({
            'rule': rule,
            'title': meta['title'],
            'severity': meta['severity'],
            'location': f'{self.rel_path}:{lineno}',
            'line': lineno,
            'evidence': evidence,
            'cause': cause,
            'fix': fix,
        })

    def visit_FunctionDef(self, node):
        self._scan_function(node)
        self.generic_visit(node)

    # Python 3.12 中 async def 同样走 visit_AsyncFunctionDef
    visit_AsyncFunctionDef = visit_FunctionDef

    def _scan_function(self, func):
        """遍历函数体（不进入嵌套函数——它们会被 visitor 单独访问），收集循环。
        R3 在这里调用一次（而不是每个循环调一次），避免重复报告。"""
        stack = [func]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue  # 嵌套函数由 generic_visit 单独扫描
                if isinstance(child, (ast.For, ast.While, ast.AsyncFor)):
                    self._scan_loop(func, child)
                stack.append(child)
        self._scan_unbounded_all(func)

    def _scan_loop(self, func, loop):
        target = getattr(loop, 'target', None)
        target_name = target.id if isinstance(target, ast.Name) else None
        seen_lines = set()

        for node in ast.walk(loop):
            if not isinstance(node, ast.Call):
                continue
            line = node.lineno
            snippet = _call_snippet(node, self.source)

            # R1：循环体内的 QuerySet 查询调用
            if _is_queryset_call(node) and line not in seen_lines:
                seen_lines.add(line)
                self._add(
                    'R1', line, snippet,
                    '循环体每次迭代都会向数据库发起一条 SQL，'
                    '迭代 N 次就产生 N 条额外查询（N+1 模式），'
                    '数据量增大后 CPU 与 DB 压力线性上升。',
                    '把查询提到循环外批量完成：\n'
                    '  # 预取外键，1 条 JOIN 搞定：\n'
                    '  qs = Model.objects.select_related("author")\n'
                    '  # 聚合统计代替循环 count：\n'
                    '  qs = qs.annotate(n=Count("replies"))',
                )

            # R2：循环体内对循环变量的多层属性访问（疑似外键懒加载）
            if target_name and line not in seen_lines:
                for child in ast.walk(node):
                    root = _fk_chain_root(child)
                    if root == target_name and isinstance(child, ast.Attribute) \
                            and child.attr not in NON_MODEL_ATTRS:
                        seen_lines.add(line)
                        self._add(
                            'R2', line,
                            _call_snippet(child, self.source) or ast.dump(child)[:80],
                            f'循环内通过 {target_name}.x.y 访问关联对象属性，'
                            '若 x 是外键且查询时未 select_related/prefetch_related，'
                            '每次访问都会触发一条额外 SQL。',
                            '在构造 QuerySet 时预取关联：\n'
                            '  qs = qs.select_related("author")        # 外键 FK\n'
                            '  qs = qs.prefetch_related("replies")     # 反向/多对多',
                        )
                        break

    def _scan_unbounded_all(self, func):
        """检测 Model.objects.all() 赋值后未切片的情况"""
        sliced_names = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                sliced_names.add(node.value.id)

        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                    and value.func.attr == 'all'
                    and isinstance(value.func.value, ast.Attribute)
                    and value.func.value.attr == 'objects'):
                continue
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in sliced_names:
                    break  # 后来被切片了，不算
            else:
                snippet = _call_snippet(value, self.source)
                self._add(
                    'R3', node.lineno, snippet,
                    '把 Model.objects.all() 的结果整体载入内存后再在 Python 层过滤/切片，'
                    '数据量大时会显著增加内存占用与响应时间（CPU 100% 的常见推手）。',
                    '把过滤条件下推到数据库并加 LIMIT：\n'
                    '  qs = Model.objects.filter(category="...")[:20]\n'
                    '  # 并在 Meta.indexes 为过滤字段加索引',
                )


def static_scan():
    """扫描项目内所有 views.py，返回按严重级别排序的静态结论

    源码未变化时复用 60 秒进程内缓存，避免每次页面访问都重新读盘 + AST 解析。
    """
    global _static_cache, _static_cache_at
    now = time.monotonic()
    if _static_cache is not None and now - _static_cache_at < 60:
        return [dict(f) for f in _static_cache]
    findings = []
    for rel_path, abs_path in _target_files():
        try:
            source = abs_path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        analyzer = ViewAnalyzer(rel_path, source)
        analyzer.visit(tree)
        findings.extend(analyzer.findings)
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f['severity'], 9), f['line']))
    _static_cache = findings
    _static_cache_at = now
    return [dict(f) for f in findings]


_static_cache = None
_static_cache_at = 0.0


# ------------------------------------------------------------------
# 动态指标诊断
# ------------------------------------------------------------------

def dynamic_scan():
    """结合 RequestMetric 采集表，给出基于真实运行数据的结论（默认看最近 24 小时）"""
    from datetime import timedelta

    from django.db.models import Avg, Count, Max, Sum
    from django.utils import timezone as tz

    from monitor.models import RequestMetric

    mon = getattr(settings, 'OBSERVABILITY', None) or settings.MONITORING
    since = tz.now() - timedelta(hours=24)
    findings = []

    # D1：平均耗时超阈值的路径
    rows = (
        RequestMetric.objects.filter(created_at__gte=since)
        .values('path')
        .annotate(n=Count('id'), avg_ms=Avg('duration_ms'), max_ms=Max('duration_ms'))
        .filter(avg_ms__gt=mon['SLOW_REQUEST_MS'] / 2)
        .order_by('-avg_ms')[:5]
    )
    for r in rows:
        findings.append({
            'rule': 'D1',
            'title': RULE_META['D1']['title'],
            'severity': RULE_META['D1']['severity'],
            'location': f'{r["path"]}（{r["n"]} 次采样）',
            'line': 0,
            'evidence': f'平均耗时 {r["avg_ms"]:.0f} ms，最慢 {r["max_ms"]:.0f} ms',
            'cause': '该路径的实测平均耗时明显高于正常水平，'
                     '结合静态扫描中的 N+1 / 全表加载结论，可互相印证。',
            'fix': '优先处理该路径上被静态规则命中的问题；'
                   '修复后用 /monitor/benchmark/ 压测对比验证收益。',
        })

    # D2：慢查询集中的路径
    slow_rows = (
        RequestMetric.objects.filter(created_at__gte=since)
        .values('path')
        .annotate(n=Count('id'), slow_total=Sum('slow_query_count'))
        .filter(slow_total__gt=0)
        .order_by('-slow_total')[:5]
    )
    for r in slow_rows:
        findings.append({
            'rule': 'D2',
            'title': RULE_META['D2']['title'],
            'severity': RULE_META['D2']['severity'],
            'location': r['path'],
            'line': 0,
            'evidence': f'累计出现 {r["slow_total"]} 条慢查询（> {mon["SLOW_QUERY_MS"]} ms）',
            'cause': '单条 SQL 超过阈值，通常是缺索引的全表扫描或一次性载入过多数据。',
            'fix': '用 django.db.connection.queries 或 silk 查看具体 SQL，'
                   '为 WHERE / ORDER BY 字段补充索引（Meta.indexes），'
                   '并检查是否有未加 LIMIT 的全表查询。',
        })
    return findings


# ------------------------------------------------------------------
# 汇总与导出
# ------------------------------------------------------------------

def full_report():
    """完整诊断报告：静态 + 动态，按严重级别排序"""
    findings = static_scan() + dynamic_scan()
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f['severity'], 9), f['line']))
    summary = {'高': 0, '中': 0, '低': 0}
    for f in findings:
        summary[f['severity']] = summary.get(f['severity'], 0) + 1
    return {'findings': findings, 'summary': summary}


def report_markdown(report):
    """把诊断报告渲染成 Markdown 文本（供 /diagnose/export/ 下载）"""
    s = report['summary']
    lines = [
        '# Django 性能自动诊断报告',
        '',
        '> 由自研规则引擎生成：静态 AST 扫描 + 采集指标动态分析。',
        '',
        f'- 严重级别 高：{s.get("高", 0)} 条',
        f'- 严重级别 中：{s.get("中", 0)} 条',
        f'- 严重级别 低：{s.get("低", 0)} 条',
        '',
        '| # | 级别 | 规则 | 问题位置 | 说明 |',
        '|---|---|---|---|---|',
    ]
    for i, f in enumerate(report['findings'], 1):
        lines.append(
            f'| {i} | {f["severity"]} | {f["rule"]} {f["title"]} '
            f'| `{f["location"]}` | {f["evidence"]} |'
        )
    lines.append('')
    lines.append('## 详细说明与修复建议')
    for i, f in enumerate(report['findings'], 1):
        lines += [
            '',
            f'### {i}. [{f["severity"]}] {f["title"]}（规则 {f["rule"]}）',
            '',
            f'- **问题位置**：`{f["location"]}`',
            f'- **证据**：`{f["evidence"]}`',
            f'- **推断原因**：{f["cause"]}',
            '- **修复建议**：',
            '',
            '```python',
            f['fix'],
            '```',
        ]
    return '\n'.join(lines) + '\n'
