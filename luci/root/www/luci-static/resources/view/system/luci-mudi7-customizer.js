'use strict';
'require view';
'require rpc';
'require poll';
'require ui';

var object = 'luci-mudi7-customizer';
var status = rpc.declare({ object: object, method: 'status', reject: true });
var setScreen = rpc.declare({ object: object, method: 'set_screen', params: ['enabled'], reject: true });
var setBattery = rpc.declare({ object: object, method: 'set_battery', params: ['enabled', 'threshold'], reject: true });
var setRuleGroup = rpc.declare({ object: object, method: 'set_rule_group', params: ['name'], reject: true });
var setLinkHost = rpc.declare({ object: object, method: 'set_link_host', params: ['host'], reject: true });
var cleanup = rpc.declare({ object: object, method: 'cleanup', params: ['confirm'], reject: true });

var preserveObject = 'luci-mudi7-customizer-preserve';
var getFeedPreserve = rpc.declare({ object: preserveObject, method: 'status', reject: true });
var setFeedPreserve = rpc.declare({ object: preserveObject, method: 'set_enabled', params: ['enabled', 'revision'], reject: true });

var batteryStates = {
	off: '限充已关闭',
	inactive: '等待限充服务',
	waiting: '等待接入 USB-C 电源',
	armed: '限充已开启，允许充电',
	held: '已达到目标，正在限制充电',
	unsupported: '设备未提供限充接口',
	error: '限充控制失败'
};

function row(label, value) {
	return E('tr', { 'class': 'tr' }, [
		E('th', { 'class': 'th', 'scope': 'row' }, label),
		E('td', { 'class': 'td' }, value)
	]);
}

function measurement(value, unit) {
	return value == null ? '—' : value + ' ' + unit;
}

function section(title, children) {
	return E('section', { 'class': 'cbi-section' }, [E('h3', {}, title)].concat(children));
}

function preserveSection() {
	var toggle = E('input', { type: 'checkbox', disabled: true });
	var feedback = E('p', { role: 'status', 'aria-live': 'polite' }, '正在读取规则…');
	var revision = '', current = false;

	function load() {
		toggle.disabled = true;
		return getFeedPreserve().then(function (result) {
			if (!result || !result.ok)
				throw new Error(result && result.error || '无法读取保留规则。');
			current = result.enabled;
			revision = result.revision;
			toggle.checked = current;
			toggle.indeterminate = result.partial;
			toggle.disabled = false;
			feedback.textContent = result.partial ? '当前仅有部分路径在规则中，开启可补全两条规则。' : '';
		}).catch(function (error) {
			feedback.textContent = error.message;
		});
	}

	toggle.addEventListener('change', function () {
		var desired = toggle.checked;
		toggle.disabled = true;
		feedback.textContent = '正在保存…';
		return setFeedPreserve(desired, revision).then(function (result) {
			if (!result || !result.ok)
				throw new Error(result && result.error || '保存失败。');
			current = result.enabled;
			revision = result.revision;
			toggle.checked = current;
			toggle.indeterminate = result.partial;
			feedback.textContent = '规则已保存。';
			toggle.disabled = false;
		}).catch(function (error) {
			toggle.checked = current;
			feedback.textContent = error.message + ' 请刷新页面后重试。';
		});
	});
	load();
	return section('系统升级软件源保留', [
		E('p', {}, 'GL.iNet 固件有升级后自动重装已有插件的功能，此功能依赖软件源。如果使用了自定义软件源，可以将 /etc/opkg/customfeeds.conf 或 /etc/opkg/customfeeds.conf.d/ 下的配置文件加入 /lib/upgrade/keep.d/opkg，以便升级时保留。'),
		E('p', {}, [E('label', {}, [toggle, ' 保留自定义软件源配置'])]),
		E('p', {}, '开关同时管理 /etc/opkg/customfeeds.conf 和 /etc/opkg/customfeeds.conf.d/ 两条规则。升级时是否恢复取决于当前版本固件的保留逻辑，请另行备份重要配置。'),
		feedback
	]);
}

return view.extend({
	load: function () {
		return L.resolveDefault(status(), null);
	},

	render: function (initial) {
		var screen = E('input', { type: 'checkbox', disabled: true });
		var battery = E('input', { type: 'checkbox', disabled: true });
		var ruleGroup = E('input', { type: 'text', disabled: true });
		var linkHost = E('input', { type: 'text', placeholder: 'www.baidu.com', disabled: true });
		var target = E('input', { type: 'number', min: 50, max: 100, step: 1, disabled: true });
		var screenLabel = E('p', {}, '正在读取屏幕状态…');
		var live = E('div', {}, '正在读取电池状态…');
		var feedback = E('p', { 'role': 'status', 'aria-live': 'polite' });
		var busy = false, available = false, dirtyEnabled = false, dirtyTarget = false, dirtyGroup = false, dirtyHost = false, revision = 0;

		function controls() {
			screen.disabled = busy || !available;
			battery.disabled = target.disabled = save.disabled = busy || !available;
			ruleGroup.disabled = saveGroup.disabled = clean.disabled = busy || !available;
			linkHost.disabled = saveHost.disabled = busy || !available;
		}

		function update(data) {
			if (!data || !data.ok)
				throw new Error(data && data.error || '无法读取设备状态。');
			var b = data.battery;
			available = true;
			screen.checked = data.screen_enabled;
			if (!dirtyGroup)
				ruleGroup.value = data.rule_group;
			if (!dirtyHost)
				linkHost.value = data.link_host;
			if (!dirtyEnabled)
				battery.checked = b.enabled;
			if (!dirtyTarget)
				target.value = b.threshold;

			screenLabel.textContent = data.screen_active ? '当前显示：第三方屏幕' :
				data.stock_active ? '当前显示：原厂屏幕' : '屏幕服务正在切换';
			var controller = batteryStates[b.control] || '未知';
			if (b.control === 'held' && b.current_ma > 0)
				controller += '，当前仍测得充电电流';
			var rows = [
				row('控制状态', controller),
				row('当前电量', b.gauge == null ? '—' : '电量计 ' + b.gauge + '% / GL 界面约 ' + b.display + '%'),
				row('USB-C 电源', b.online == null ? '—' : b.online ? '已接入' : '未接入'),
				row('电池电压', measurement(b.voltage_mv, 'mV')),
				row('电池电流', b.current_ma == null ? '—' : measurement(b.current_ma, 'mA') + '（正值表示充电，负值表示放电，零值表示电池未工作）'),
				row('已保存阈值', b.threshold + '%（GL 刻度）' +
					(b.gauge_stop == null ? '' : '，约电量计 ' + b.gauge_stop + '% 停止，低于此值恢复'))
			];
			live.replaceChildren(E('table', { 'class': 'table' }, rows));
			controls();
		}

		function refresh() {
			if (busy)
				return Promise.resolve();
			var started = revision;
			return status().then(function (data) {
				if (started === revision && !busy)
					update(data);
			}).catch(function (error) {
				if (started === revision && !busy) {
					available = false;
					feedback.textContent = error.message;
					controls();
				}
			});
		}

		function apply(action, message, resetInputs) {
			if (busy)
				return Promise.resolve();
			busy = true;
			revision++;
			controls();
			feedback.textContent = '正在应用…';
			return Promise.resolve().then(action).then(function (data) {
				if (!data || !data.ok)
					throw new Error(data && data.error || '操作失败。');
				if (resetInputs)
					resetInputs();
				update(data);
				feedback.textContent = message;
			}).catch(function (error) {
				feedback.textContent = error.message;
			}).finally(function () {
				busy = false;
				controls();
				return refresh();
			});
		}

		screen.addEventListener('change', function () {
			var enabled = screen.checked;
			apply(function () { return setScreen(enabled); }, enabled ? '第三方屏幕已启用。' : '已切回原厂屏幕。');
		});
		battery.addEventListener('change', function () { dirtyEnabled = true; });
		target.addEventListener('input', function () { dirtyTarget = true; });

		var save = E('button', { 'class': 'btn cbi-button cbi-button-apply', type: 'button', disabled: true }, '应用限充设置');
		save.addEventListener('click', function () {
			var threshold = Number(target.value), enabled = battery.checked;
			if (!Number.isInteger(threshold) || threshold < 50 || threshold > 100) {
				feedback.textContent = '请输入 50–100 的整数。';
				return;
			}
			apply(function () { return setBattery(enabled, threshold); }, '限充设置已应用。', function () {
				dirtyEnabled = dirtyTarget = false;
			});
		});

		ruleGroup.addEventListener('input', function () { dirtyGroup = true; });
		var saveGroup = E('button', { 'class': 'btn cbi-button cbi-button-apply', type: 'button', disabled: true }, '保存策略组');
		saveGroup.addEventListener('click', function () {
			var name = ruleGroup.value;
			if (!name.trim()) {
				feedback.textContent = '请输入策略组名称。';
				return;
			}
			apply(function () { return setRuleGroup(name); }, '策略组已保存，触屏将在下一次刷新时读取。', function () {
				dirtyGroup = false;
			});
		});

		linkHost.addEventListener('input', function () { dirtyHost = true; });
		var saveHost = E('button', { 'class': 'btn cbi-button cbi-button-apply', type: 'button', disabled: true }, '保存探测地址');
		saveHost.addEventListener('click', function () {
			var host = linkHost.value.trim();
			if (!host) {
				feedback.textContent = '请输入 IPv4 / IPv6 地址或域名。';
				return;
			}
			if (host.length > 253) {
				feedback.textContent = '探测地址过长（最多 253 个字符）。';
				return;
			}
			apply(function () { return setLinkHost(host); }, '探测地址已保存，Monitor 页将在下一次探测时使用（约 3 秒）。', function () {
				dirtyHost = false;
			});
		});

		var clean = E('button', { 'class': 'btn cbi-button cbi-button-negative', type: 'button', disabled: true }, '卸载前清理');
		clean.addEventListener('click', function () {
			ui.showModal('卸载前清理', [
				E('p', {}, '此操作会切回原厂屏幕，停止第三方屏幕、按键切换和限充服务，并删除亮度、PIN、限充设置、链路探测地址和运行数据。清理后可在软件包管理中卸载本插件。'),
				E('div', { 'class': 'right' }, [
					E('button', { 'class': 'btn', type: 'button', click: ui.hideModal }, '取消'), ' ',
					E('button', {
						'class': 'btn cbi-button cbi-button-negative', type: 'button', click: function () {
							ui.hideModal();
							return apply(function () { return cleanup(true); }, '清理完成。现在可以卸载 luci-mudi7-customizer。', function () {
								dirtyEnabled = dirtyTarget = dirtyGroup = dirtyHost = false;
							});
						}
					}, '确认清理')
				])
			]);
		});

		try { update(initial); }
		catch (error) { feedback.textContent = error.message; }
		poll.add(refresh, 5);

		return E('div', {}, [
			E('h2', {}, 'Mudi7 个性化'),
			section('第三方屏幕', [
				E('label', {}, [screen, ' 启用第三方屏幕']),
				E('p', {}, '开启后切到第三方屏幕，并启用原厂与第三方屏幕之间的按键切换（长按 Home 键 2 秒左右切换）。关闭后由原厂屏幕处理按键。'),
				screenLabel,
				E('hr'),
				E('p', {}, [E('strong', {}, 'OpenClash 触屏设置')]),
				E('p', {}, [E('label', {}, ['Rule 模式策略组 ', ruleGroup]), ' ', saveGroup]),
				E('p', {}, '填写 Mihomo 配置中的完整手动选择组名称，区分大小写。默认是「默认代理」。Global 模式使用 GLOBAL。'),
				E('hr'),
				E('p', {}, [E('strong', {}, 'Monitor 链路探测')]),
				E('p', {}, [E('label', {}, ['探测地址 ', linkHost]), ' ', saveHost]),
				E('p', {}, 'Monitor 页的 Link Health 会向此地址发送 ICMP 探测：可填 IPv4（如 1.1.1.1）、IPv6（如 2400:3200::1）或域名（如 www.baidu.com，按 IPv4 探测），默认 www.baidu.com。探测只在 Monitor 页显示时进行，离开该页即停止。保存后约 3 秒内生效，无需重启屏幕服务。')
			]),
			section('电池限充', [
				E('p', {}, [E('label', {}, [battery, ' 启用电池限充'])]),
				E('p', {}, [E('label', {}, ['充电目标（GL 界面刻度，50–100%） ', target]), ' ', save]),
				E('p', {}, '与第三方触屏共用设置。切回原厂屏幕后，电池限充可继续工作。')
			]),
			section('电池状态', [live]),
			preserveSection(),
			section('清理工具', [clean]),
			feedback
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
