"""
monitor/geoip.py — IP 归属地解析（可插拔：真实 IP 库优先，内置演示解析兜底）

解析优先级：
1. geoip2 + GeoLite2-City.mmdb（MaxMind 免费库，把 mmdb 放到项目根目录即可）
2. qqwry-py3 + qqwry.dat（纯真免费库，把 dat 放到项目根目录即可）
3. 内置确定性演示解析：按 IP 哈希稳定映射到省份/省会（同一 IP 结果恒定），
   仅用于演示与教学——生产请接入上面两个真实库之一。

所有结果带进程内 LRU 缓存，避免每个请求重复解析。
省份命名与 ECharts 中国地图 GeoJSON 一致（短名，如 "广东"、"内蒙古"）。
"""
import ipaddress
import threading

_PROVINCE_TABLE = [
    # (省份[ECharts短名], 流量权重%, 代表城市)
    ('广东', 13.0, '深圳'), ('江苏', 8.5, '南京'), ('浙江', 8.0, '杭州'),
    ('北京', 7.5, '北京'), ('山东', 6.5, '青岛'), ('上海', 7.0, '上海'),
    ('河南', 5.5, '郑州'), ('四川', 5.0, '成都'), ('湖北', 4.5, '武汉'),
    ('湖南', 4.0, '长沙'), ('福建', 3.8, '厦门'), ('河北', 3.5, '石家庄'),
    ('安徽', 3.2, '合肥'), ('陕西', 3.0, '西安'), ('辽宁', 2.8, '沈阳'),
    ('重庆', 2.6, '重庆'), ('江西', 2.2, '南昌'), ('广西', 1.9, '南宁'),
    ('天津', 2.0, '天津'), ('云南', 1.8, '昆明'), ('山西', 1.6, '太原'),
    ('吉林', 1.4, '长春'), ('贵州', 1.3, '贵阳'), ('黑龙江', 1.2, '哈尔滨'),
    ('内蒙古', 1.1, '呼和浩特'), ('新疆', 0.8, '乌鲁木齐'), ('甘肃', 0.7, '兰州'),
    ('海南', 0.6, '海口'), ('宁夏', 0.4, '银川'), ('青海', 0.3, '西宁'),
    ('西藏', 0.2, '拉萨'), ('香港', 0.3, '香港'), ('台湾', 0.3, '台北'),
    ('澳门', 0.1, '澳门'),
]

# ECharts china.json 的省份名 -> 标准化（把全名归一为短名，兼容真实库输出）
_NAME_ALIASES = {
    '内蒙古自治区': '内蒙古', '广西壮族自治区': '广西', '西藏自治区': '西藏',
    '宁夏回族自治区': '宁夏', '新疆维吾尔自治区': '新疆',
    '北京市': '北京', '上海市': '上海', '天津市': '天津', '重庆市': '重庆',
    '河北省': '河北', '山西省': '山西', '辽宁省': '辽宁', '吉林省': '吉林',
    '黑龙江省': '黑龙江', '江苏省': '江苏', '浙江省': '浙江', '安徽省': '安徽',
    '福建省': '福建', '江西省': '江西', '山东省': '山东', '河南省': '河南',
    '湖北省': '湖北', '湖南省': '湖南', '广东省': '广东', '海南省': '海南',
    '四川省': '四川', '贵州省': '贵州', '云南省': '云南', '陕西省': '陕西',
    '甘肃省': '甘肃', '青海省': '青海', '台湾省': '台湾',
    '香港特别行政区': '香港', '澳门特别行政区': '澳门',
}

# 海外国家（名字与 ECharts world.json 一致），权重为占总流量的百分比
_OVERSEAS_TABLE = [
    ('United States', 1.6), ('Japan', 1.2), ('Singapore', 0.7), ('Korea', 0.6),
    ('India', 0.6), ('Germany', 0.5), ('United Kingdom', 0.5),
    ('Australia', 0.5), ('Canada', 0.5), ('Russia', 0.4), ('France', 0.3),
    ('Netherlands', 0.3), ('Brazil', 0.3), ('Vietnam', 0.3), ('Thailand', 0.3),
    ('Malaysia', 0.3), ('Indonesia', 0.3), ('Philippines', 0.2), ('Italy', 0.2),
    ('Spain', 0.2),
]
_OVERSEAS_TOTAL = sum(w for _, w in _OVERSEAS_TABLE)

# 真实库输出常见 ISO 代码 / 中文名 -> world.json 英文名
_ISO_TO_NAME = {
    'US': 'United States', 'JP': 'Japan', 'SG': 'Singapore', 'KR': 'Korea',
    'IN': 'India', 'DE': 'Germany', 'GB': 'United Kingdom', 'AU': 'Australia',
    'CA': 'Canada', 'RU': 'Russia', 'FR': 'France', 'NL': 'Netherlands',
    'BR': 'Brazil', 'VN': 'Vietnam', 'TH': 'Thailand', 'MY': 'Malaysia',
    'ID': 'Indonesia', 'PH': 'Philippines', 'IT': 'Italy', 'ES': 'Spain',
}
_CN_COUNTRY_TO_NAME = {
    '美国': 'United States', '日本': 'Japan', '新加坡': 'Singapore',
    '韩国': 'Korea', '印度': 'India', '德国': 'Germany', '英国': 'United Kingdom',
    '澳大利亚': 'Australia', '加拿大': 'Canada', '俄罗斯': 'Russia',
    '法国': 'France', '荷兰': 'Netherlands', '巴西': 'Brazil', '越南': 'Vietnam',
    '泰国': 'Thailand', '马来西亚': 'Malaysia', '印尼': 'Indonesia',
    '印度尼西亚': 'Indonesia', '菲律宾': 'Philippines', '意大利': 'Italy',
    '西班牙': 'Spain',
}

_lock = threading.Lock()
_cache = {}  # ip -> (province, city, source)
_cache_max = 8000

_geo_reader = None       # geoip2 Reader 实例（惰性初始化，失败置 False）
_qqwry_obj = None        # QQwry 实例
_backend_checked = False
_backend_name = ''


def _init_backend():
    """探测可用的真实 IP 库（加锁只做一次，避免并发首请求创建多个 Reader）"""
    global _geo_reader, _qqwry_obj, _backend_checked, _backend_name
    with _lock:
        if _backend_checked:
            return
        _backend_checked = True
        from pathlib import Path
        try:
            from django.conf import settings
            base = Path(settings.BASE_DIR)
        except Exception:
            base = Path('.')

        mmdb = base / 'GeoLite2-City.mmdb'
        if mmdb.exists():
            try:
                import geoip2.database
                _geo_reader = geoip2.database.Reader(str(mmdb))
                _backend_name = 'GeoLite2'
                return
            except Exception:
                _geo_reader = None

        dat = base / 'qqwry.dat'
        if dat.exists():
            try:
                from qqwry import QQwry
                q = QQwry()
                if q.load_file(str(dat)):
                    _qqwry_obj = q
                    _backend_name = 'QQwry'
                    return
            except Exception:
                _qqwry_obj = None


def _is_private(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_loopback or a.is_reserved or a.is_link_local
    except ValueError:
        return True


def _resolve_geoip2(ip):
    try:
        resp = _geo_reader.city(ip)
        country = (resp.country.iso_code or '')
        if country != 'CN':
            name = _ISO_TO_NAME.get(country, country or '未知')
            return ('海外', name)
        # 中国：取省（subdivisions[0]），归一为 ECharts 短名
        # 注意 geoip2 的属性名是 subdivisions（不是 submissions）
        subs = getattr(resp, 'subdivisions', None)
        raw = ''
        if subs and len(subs) > 0:
            raw = subs[0].names.get('zh-CN') or subs[0].name or ''
        prov = _NAME_ALIASES.get(raw, raw[:3] if raw else '')
        city = ''
        if resp.city and resp.city.names:
            city = resp.city.names.get('zh-CN') or resp.city.name or ''
        if prov:
            return (prov, city[:20])
        return ('中国', city[:20] or '')
    except Exception:
        return None


def _resolve_qqwry(ip):
    try:
        text = _qqwry_obj.lookup(ip) or ''
        head = text.split(' ')[0]
        # 先判国家（中文 -> world.json 英文名）
        for cn, en in _CN_COUNTRY_TO_NAME.items():
            if head.startswith(cn):
                return ('海外', en)
        prov = ''
        for full, short in _NAME_ALIASES.items():
            if head.startswith(full[:2]):
                prov = short
                break
        if not prov:
            for p, _, _ in _PROVINCE_TABLE:
                if head.startswith(p):
                    prov = p
                    break
        if prov:
            # 剥掉省名后紧跟的行政区划后缀（省/市/自治区/特别行政区）
            rest = head[len(prov):]
            for suffix in ('壮族自治区', '回族自治区', '维吾尔自治区', '自治区', '特别行政区', '省', '市'):
                if rest.startswith(suffix):
                    rest = rest[len(suffix):]
                    break
            return (prov, rest[:20] or prov)
        if head:
            return ('海外', text[:20] or '未知')
        return ('未知', '')
    except Exception:
        return None


def _resolve_demo(ip):
    """确定性演示解析：把 IP 映射为稳定的 (省份/海外, 国家或城市)"""
    try:
        n = int(ipaddress.ip_address(ip))
    except ValueError:
        return ('未知', '')
    # 用 IP 数值做确定性哈希
    h = (n * 2654435761 + 1013904223) % (2 ** 32)
    frac = h / (2 ** 32)
    # 约 8.5% 为海外流量（与真实站点的海外占比量级接近）
    if frac < 0.085:
        f2 = (h // 97 % 10000) / 10000.0
        acc = 0.0
        for name, w in _OVERSEAS_TABLE:
            acc += w / _OVERSEAS_TOTAL
            if f2 < acc:
                return ('海外', name)
        return ('海外', 'United States')
    # 国内：剩余区间按省份权重落位
    f2 = (frac - 0.085) / 0.915
    acc = 0.0
    total = sum(w for _, w, _ in _PROVINCE_TABLE)
    for prov, weight, city in _PROVINCE_TABLE:
        acc += weight / total
        if f2 < acc:
            return (prov, city)
    return ('未知', '')


def resolve(ip):
    """解析 IP -> (省份, 城市, 数据来源)；内网 IP 返回 ('局域网', '', 'local')"""
    if not ip:
        return ('未知', '', 'none')
    with _lock:
        if ip in _cache:
            return _cache[ip]
    if _is_private(ip):
        result = ('局域网', '', 'local')
    else:
        _init_backend()
        result = None
        if _geo_reader:
            result = _resolve_geoip2(ip)
        elif _qqwry_obj:
            result = _resolve_qqwry(ip)
        if not result:
            prov, city = _resolve_demo(ip)
            result = (prov, city, 'demo')
    with _lock:
        if len(_cache) >= _cache_max:
            # 逐出最早写入的一半条目（避免 clear() 造成的缓存悬崖：瞬时全部回源）
            for old in list(_cache.keys())[:_cache_max // 2]:
                _cache.pop(old, None)
        _cache[ip] = result
    return result


def backend_name():
    _init_backend()
    return _backend_name or '内置演示解析'


def province_names():
    return [p for p, _, _ in _PROVINCE_TABLE]
