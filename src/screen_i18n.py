"""Third-party screen captions; no translation of user-provided proxy names."""
import os
from pathlib import Path
import re
import time

LANGUAGE_FILE = Path('/etc/mudi7-customizer/screen-language')
DEFAULT_LANGUAGE = 'zh-cn'
_language = DEFAULT_LANGUAGE
_checked_at = -1e9


def get_language():
    try:
        value = LANGUAGE_FILE.read_text().strip()
        return value if value in ('zh-cn', 'en') else DEFAULT_LANGUAGE
    except OSError:
        return DEFAULT_LANGUAGE


def set_language(value):
    if value not in ('zh-cn', 'en'):
        raise ValueError('Choose Chinese or English for the custom screen.')
    LANGUAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = LANGUAGE_FILE.with_suffix('.new')
    with temp.open('w') as stream:
        stream.write(value + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(LANGUAGE_FILE)
    return value


def refresh_language(force=False):
    global _language, _checked_at
    now = time.monotonic()
    if not force and now - _checked_at < 1:
        return False
    _checked_at = now
    value = get_language()
    changed = value != _language
    _language = value
    return changed


def chinese():
    return _language == 'zh-cn'


ZH = {'Starting...': '正在启动…',
 'No connection to the test server': '无法连接测速服务器',
 'Could not read GL settings': '无法读取 GL 设置',
 'Unsupported GL response': '不支持此 GL 响应',
 'Unsupported acceleration mode': '不支持此加速模式',
 'Enable acceleration to change mode': '请先启用加速再切换模式',
 'Mode is unavailable': '此模式不可用',
 'GL rejected the change': 'GL 未接受此更改',
 'Could not verify GL settings': '无法验证 GL 设置',
 'BATTERY': '电池',
 'CELLULAR': '蜂窝网络',
 'MONITOR': '系统监控',
 'SETTINGS': '设置',
 'Screen PIN': '屏幕密码',
 'LOCKED': '已锁定',
 'USB power': 'USB 供电',
 'On battery': '电池供电',
 'Mudi 7 scale': 'Mudi 7 电量刻度',
 'Charge limit': '充电限制',
 'Target': '目标电量',
 'Battery current': '电池电流',
 'Service unavailable': '服务不可用',
 'Charge held': '已暂停充电',
 'Armed': '限充保护中',
 'Limit off': '未启用限充',
 'Charge limit reached': '已达到充电上限',
 'Charge setting applied': '充电设置已应用',
 'Protection on': '已启用密码保护',
 'Protection off': '未启用密码保护',
 'Change PIN': '修改密码',
 'Set PIN and enable': '设置并启用密码',
 'Turn off protection': '关闭密码保护',
 'Enter PIN': '输入密码',
 'Enter current PIN': '输入当前密码',
 'Set a four-digit PIN': '设置四位数字密码',
 'Confirm new PIN': '再次输入新密码',
 'Del': '删除',
 'Incorrect PIN': '密码错误',
 'Wait 30 seconds': '请等待 30 秒',
 'PINs did not match': '两次密码不一致',
 'Could not disable PIN': '无法关闭密码保护',
 'Could not save PIN': '无法保存密码',
 'No service': '无服务',
 'Connected': '已连接',
 'Disconnected': '未连接',
 'QUALITY': '信号质量',
 'Good': '良好',
 'Fair': '一般',
 'Poor': '较差',
 'PCI / Channel': 'PCI / 信道',
 'TAC / Cell ID': 'TAC / 小区 ID',
 'Primary BW': '主载波带宽',
 'No serving cell': '无服务小区',
 'Mode': '模式',
 'Global': '全局',
 'Rule': '规则',
 'Global Node': '全局节点',
 'Default Proxy': '默认代理',
 'Rule Group': '规则分组',
 'Traffic (session)': '本次运行流量',
 'Mihomo core': 'Mihomo 内核',
 "OpenClash isn't installed": '此设备尚未安装',
 'on this device': 'OpenClash',
 'API unavailable': 'API 不可用',
 'Mihomo API unavailable': 'Mihomo API 不可用',
 'Core stopped': '内核未运行',
 'Selector unavailable': '代理选择器不可用',
 'No selection': '尚未选择',
 'No choices available': '没有可选节点',
 'Started': '已启动',
 'Stopped': '已停止',
 'Enabled, but not running': '已启用，内核未运行',
 'Stopping…': '正在停止…',
 'Starting…': '正在启动…',
 'Running': '运行中',
 'Running (disabled in config)': '运行中（配置已停用）',
 'Checking': '检测中',
 'Link Health': '链路状态',
 'Latency': '延迟',
 'Packet Loss': '丢包率',
 'Temp': '温度',
 'Uptime': '运行时间',
 'Speed test': '网络测速',
 'Reachable': '正常',
 'No reply': '无响应',
 'No WAN': '无外网连接',
 'CPU': 'CPU',
 'RAM': 'RAM',
 'Download': '下载',
 'Upload': '上传',
 'Cancel': '取消',
 'Ready': '就绪',
 'Start': '开始',
 'Stop': '停止',
 'Run again': '重新测速',
 'Test failed': '测速失败',
 'Cancelled': '已取消',
 '~12s · Cloudflare · uses up to ~330 MB': '约 12 秒 · Cloudflare · 最多约 330 MB',
 'Yes': '确定',
 'OK': '知道了',
 'Network Accel.': '网络加速',
 'Screen': '屏幕设置',
 'Stock UI': '原厂屏幕',
 'Return': '切换',
 'Reboot': '重启',
 'Shutdown': '关机',
 'On': '已开启',
 'Off': '已关闭',
 'Unavailable': '不可用',
 'Acceleration': '网络加速',
 'Brightness': '屏幕亮度',
 'Screen timeout': '休眠时间',
 'Never': '从不',
 '15 seconds': '15 秒',
 '30 seconds': '30 秒',
 '1 minute': '1 分钟',
 '2 minutes': '2 分钟',
 '5 minutes': '5 分钟',
 '10 minutes': '10 分钟',
 'Hand the screen back to the GL.iNet UI?': '切换回 GL.iNet 原厂屏幕？',
 'Switch': '切换',
 'Reboot the router now?': '现在重启路由器？',
 'Shut down the router now?': '现在关闭路由器？',
 'Restarting…': '正在重启…',
 'Powering off': '正在关机',
 'Back in about a minute': '约一分钟后恢复运行',
 'The screen will go dark': '屏幕即将熄灭',
 'Shutting down…': '正在关机…',
 'Please wait': '请稍候',
 "Don't unplug until it's off": '关机完成前请勿断电',
 "Start OpenClash? All traffic will go through the proxy once it's up.": '启动 '
                                                                         'OpenClash？启动后流量将按代理规则转发。',
 'Stop OpenClash? Traffic will go direct, without the proxy.': '停止 OpenClash？停止后流量将直接连接。',
 "Couldn't read OpenClash's state back": '无法读取 OpenClash 状态',
 'Applying acceleration…': '正在设置网络加速…',
 'Acceleration request failed': '网络加速设置失败',
 'Conflict': '设置冲突',
 'Switching proxy…': '正在切换代理…',
 'Could not verify selection': '无法验证代理选择',
 'Could not save screen setting': '无法保存屏幕设置',
 'Mode changed. Open the picker again.': '模式已变化，请重新打开选择器。',
 'Group changed. Open the picker again.': '分组已变化，请重新打开选择器。',
 'Could not switch proxy': '无法切换代理',
 'Applying charge setting...': '正在设置充电限制…',
 "Reboot didn't start": '未能启动重启操作',
 "Shutdown didn't start": '未能启动关机操作',
 'Switching to stock UI…': '正在切换原厂屏幕…',
 'Reading GL settings…': '正在读取 GL 设置…',
 'GL acceleration unavailable': 'GL 网络加速不可用',
 'Checking settings…': '正在检查设置…',
 'Changing mode…': '正在切换模式…',
 'Software': '软件加速',
 'Hardware': '硬件加速',
 'None': '无',
 'Repeater': '无线中继',
 'Cellular': '蜂窝网络',
 'Ethernet': '有线网络',
 'No connection': '未连接',
 'Auto': '自动',
 'OpenClash is enabled. Turn off OpenClash before enabling network acceleration.': 'OpenClash '
                                                                                   '已启用，请先关闭再开启网络加速。',
 'Network acceleration is enabled. Turn it off before starting OpenClash.': '网络加速已启用，请先关闭再启动 '
                                                                            'OpenClash。',
 'Could not check network acceleration. Try again.': '无法检查网络加速，请重试。',
 'OpenClash is not installed': '尚未安装 OpenClash',
 'touch input unavailable': '触摸输入不可用',
 'touch input stopped': '触摸输入已停止',
 'Timed out': '请求超时',
 'Download failed': '下载测速失败',
 'Upload failed': '上传测速失败'}

ZH.update({
    'Service': '服务总开关',
    'Not installed': '未安装',
    'Status unavailable': '状态不可用',
    'Connecting…': '正在连接…',
    'Login required': '需要登录',
    'Authorization required': '需要授权',
    'Requests': '请求数',
    'Blocked': '拦截数',
    'Last %s day': '最近 %s 天',
    'Last %s days': '最近 %s 天',
    'Statistics period unavailable': '统计周期不可用',
    'Statistics authentication failed': '统计接口认证失败',
    'Statistics unavailable': '统计不可用',
    'Client DNS handling enabled': '已启用客户端 DNS 处理',
    'Client DNS handling disabled': '未启用客户端 DNS 处理',
    'Tailscale address': 'Tailscale 地址',
    'Log in to Tailscale in the WebUI before using it here.': '请先在 WebUI 登录 Tailscale，再使用屏幕控制。',
    'Authorize this device in the Tailscale WebUI.': '请在 Tailscale WebUI 中完成此设备的授权。',
    'Tailscale is off.': 'Tailscale 已关闭',
    'Complete login in the WebUI.': '请在 WebUI 完成登录。',
    'Manage network settings in the WebUI.': '网络设置请使用 WebUI。',
    'Start AdGuard Home? DNS settings may change.': '启动 AdGuard Home？这会联动调整 DNS 设置。',
    'Stop AdGuard Home? DNS settings may change.': '停止 AdGuard Home？这会联动调整 DNS 设置。',
    'Start Tailscale? If login is required, complete it in the WebUI.': '启动 Tailscale？如需登录，请在 WebUI 完成。',
    'Stop Tailscale? Remote access may disconnect.': '停止 Tailscale？远程连接可能中断。',
    'Applying service setting…': '正在应用服务设置…',
    'Official service settings are unavailable.': '无法读取原厂服务设置。',
    'Unsupported official service settings. Check for plugin updates.': '不支持此原厂服务配置，请检查插件更新。',
    'Official service operation timed out. Check its actual status.': '原厂服务操作超时，请检查实际状态。',
    'The official service rejected the change. Check its settings in the WebUI.': '原厂服务拒绝了更改，请在 WebUI 检查设置。',
    'The service has not reached the requested state. Check its status in the WebUI.': '服务未达到预期状态，请在 WebUI 检查。',
})

PATTERNS = [
    (r'Slot (.+) / Data (.+)', lambda m: f'卡槽 {m[1]} / 数据 {tr(m[2])}'),
    (r'(\d+) carriers?( / .+ MHz)?', lambda m: f'{m[1]} 个载波{m[2] or ""}'),
    (r'via (.+) · (\d+) MB used', lambda m: f'经 {tr(m[1])} · 已用 {m[2]} MB'),
    (r'(\d+) MB used', lambda m: f'已用 {m[1]} MB'),
    (r'(\d+)d (\d+)h', lambda m: f'{m[1]}天{m[2]}时'),
    (r'(\d+)h (\d+)m', lambda m: f'{m[1]}时{m[2]}分'),
    (r'(Starting|Stopping) OpenClash… (\d+)s', lambda m: f'{"正在启动" if m[1] == "Starting" else "正在停止"} OpenClash… {m[2]}秒'),
    (r"OpenClash didn't come up within (\d+)s -- check its log in LuCI", lambda m: f'OpenClash 在 {m[1]} 秒内未启动，请检查 LuCI 日志'),
    (r'OpenClash still running after (\d+)s', lambda m: f'{m[1]} 秒后 OpenClash 仍在运行'),
    (r'Retry in (\d+)s', lambda m: f'{m[1]} 秒后重试'),
]


def tr(text):
    if not chinese() or not isinstance(text, str):
        return text
    if text in ZH:
        return ZH[text]
    for pattern, translate in PATTERNS:
        match = re.fullmatch(pattern, text)
        if match:
            return translate(match)
    return text


refresh_language(True)
