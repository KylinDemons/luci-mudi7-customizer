'use strict';
'require view';
'require rpc';
'require poll';
'require ui';

var object = 'luci-mudi7-customizer';
var status = rpc.declare({ object: object, method: 'status', reject: true });
var setStockI18n = rpc.declare({ object: object, method: 'stock_i18n', params: ['action'], reject: true });
var setScreenModules = rpc.declare({ object: object, method: 'set_screen_modules', params: ['modules'], reject: true });
var setScreenLanguage = rpc.declare({ object: object, method: 'set_screen_language', params: ['language'], reject: true });
var setScreen = rpc.declare({ object: object, method: 'set_screen', params: ['enabled'], reject: true });
var setBattery = rpc.declare({ object: object, method: 'set_battery', params: ['enabled', 'threshold'], reject: true });
var setRuleGroup = rpc.declare({ object: object, method: 'set_rule_group', params: ['name'], reject: true });
var setLinkHost = rpc.declare({ object: object, method: 'set_link_host', params: ['host'], reject: true });
var cleanup = rpc.declare({ object: object, method: 'cleanup', params: ['confirm'], reject: true });

var preserveObject = 'luci-mudi7-customizer-preserve';
var getFeedPreserve = rpc.declare({ object: preserveObject, method: 'status', reject: true });
var setFeedPreserve = rpc.declare({ object: preserveObject, method: 'set_enabled', params: ['enabled', 'revision'], reject: true });

var batteryStates = {
	off: _("Charge limit disabled"),
	inactive: _("Waiting for the charge limit service"),
	waiting: _("Waiting for USB-C power"),
	armed: _("Charge limit enabled, charging allowed"),
	held: _("Target reached, charging limited"),
	unsupported: _("Charge limit interface unavailable"),
	error: _("Charge limit control failed")
};

function rpcError(result, fallback) {
	if (!result || !result.error)
		return fallback;
	var params = (result.error_params || []).map(function (value) {
		return Array.isArray(value) ? value.map(function (label) { return _(label); }).join(', ') :
			typeof value === 'string' ? _(value) : value;
	});
	return String.prototype.format.apply(_(result.error), params);
}

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
	var feedback = E('p', { role: 'status', 'aria-live': 'polite' }, _("Loading rules…"));
	var revision = '', current = false;

	function load() {
		toggle.disabled = true;
		return getFeedPreserve().then(function (result) {
			if (!result || !result.ok)
				throw new Error(rpcError(result, _("Unable to read preservation rules.")));
			current = result.enabled;
			revision = result.revision;
			toggle.checked = current;
			toggle.indeterminate = result.partial;
			toggle.disabled = false;
			feedback.textContent = result.partial ? _("Only some paths are currently listed. Enable this option to add both rules.") : '';
		}).catch(function (error) {
			feedback.textContent = error.message;
		});
	}

	toggle.addEventListener('change', function () {
		var desired = toggle.checked;
		toggle.disabled = true;
		feedback.textContent = _("Saving…");
		return setFeedPreserve(desired, revision).then(function (result) {
			if (!result || !result.ok)
				throw new Error(rpcError(result, _("Save failed.")));
			current = result.enabled;
			revision = result.revision;
			toggle.checked = current;
			toggle.indeterminate = result.partial;
			feedback.textContent = _("Rules saved.");
			toggle.disabled = false;
		}).catch(function (error) {
			toggle.checked = current;
			feedback.textContent = error.message + ' ' + _("Please refresh the page and try again.");
		});
	});
	load();
	return section(_("Preserve package feeds during firmware upgrades"), [
		E('p', {}, _("GL.iNet firmware can automatically reinstall existing plugins after an upgrade. This requires package feeds. If you use custom feeds, you can add /etc/opkg/customfeeds.conf or the configuration files under /etc/opkg/customfeeds.conf.d/ to /lib/upgrade/keep.d/opkg to preserve them during upgrades.")),
		E('p', {}, [E('label', {}, [toggle, ' ', _("Preserve custom package feed configuration")])]),
		E('p', {}, _("This switch manages both /etc/opkg/customfeeds.conf and /etc/opkg/customfeeds.conf.d/. Restoration during upgrades depends on the preservation logic in the current firmware version. Back up important settings separately.")),
		feedback
	]);
}

return view.extend({
	load: function () {
		return L.resolveDefault(status(), null);
	},

	render: function (initial) {
		var stockState = E('p', { role: 'status', 'aria-live': 'polite' }, _("Loading stock UI status…"));
		var stockWarning = E('p', { 'class': 'alert-message warning', role: 'alert', 'aria-live': 'polite', style: 'display: none' });
		var stockData = {};
		var screenLanguage = E('select', { disabled: true }, [
			E('option', { value: 'zh-cn' }, _("Chinese")),
			E('option', { value: 'en' }, _("English"))
		]);
		var moduleItems = [
			['sim', _("Cellular")], ['monitor', _("Monitor")], ['battery', _("Battery")],
			['openclash', 'OpenClash'], ['adguardhome', 'AdGuard Home'],
			['tailscale', 'Tailscale'], ['settings', _("Settings")]
		];
		var moduleInputs = {}, selectedModules = moduleItems.map(function (item) { return item[0]; });
		var moduleChoices = E('div', { role: 'group', 'aria-label': _("Visible screen modules") }, moduleItems.map(function (item) {
			var input = E('input', { type: 'checkbox', disabled: true, 'data-screen-module': item[0] });
			moduleInputs[item[0]] = input;
			return E('label', { style: 'display: inline-block; margin-right: 1.2em; margin-bottom: .5em' }, [input, ' ', item[1]]);
		}));
		var screen = E('input', { type: 'checkbox', disabled: true });
		var battery = E('input', { type: 'checkbox', disabled: true });
		var ruleGroup = E('input', { type: 'text', disabled: true });
		var linkHost = E('input', { type: 'text', placeholder: 'www.baidu.com', disabled: true });
		var target = E('input', { type: 'number', min: 50, max: 100, step: 1, disabled: true });
		var screenLabel = E('p', {}, _("Loading screen status…"));
		var live = E('div', {}, _("Loading battery status…"));
		var feedback = E('p', { 'role': 'status', 'aria-live': 'polite' });
		var busy = false, available = false, dirtyEnabled = false, dirtyTarget = false, dirtyGroup = false, dirtyHost = false, revision = 0;

		function controls() {
			screen.disabled = screenLanguage.disabled = busy || !available;
			moduleItems.forEach(function (item) {
				var input = moduleInputs[item[0]];
				input.disabled = busy || !available || (input.checked && selectedModules.length === 1);
			});
			applyStock.disabled = busy || !available || !stockData.supported || stockData.state !== 'original';
			restoreStock.disabled = busy || !available || !stockData.can_restore;
			battery.disabled = target.disabled = save.disabled = busy || !available;
			ruleGroup.disabled = saveGroup.disabled = clean.disabled = busy || !available;
			linkHost.disabled = saveHost.disabled = busy || !available;
		}

		function update(data) {
			if (!data || !data.ok)
				throw new Error(rpcError(data, _("Unable to read device status.")));
			var b = data.battery;
			stockData = data.stock_i18n || {};
			stockState.textContent = _("Current status") + ': ' + (stockData.state === 'applied' ? _("Chinese localization applied") :
				stockData.state === 'original' ? _("Original stock UI") : _("Unknown (resources changed or backup incomplete)"));
			if (!stockData.supported)
				stockState.textContent += '. ' + _(stockData.reason || "The stock UI resource format or device is not supported. Check for plugin updates.");
			stockWarning.textContent = stockData.warning ? _(stockData.warning) : '';
			stockWarning.style.display = stockData.warning ? '' : 'none';
			available = true;
			screen.checked = data.screen_enabled;
			screenLanguage.value = data.screen_language || 'zh-cn';
			selectedModules = data.screen_modules || moduleItems.map(function (item) { return item[0]; });
			moduleItems.forEach(function (item) { moduleInputs[item[0]].checked = selectedModules.indexOf(item[0]) !== -1; });
			if (!dirtyGroup)
				ruleGroup.value = data.rule_group;
			if (!dirtyHost)
				linkHost.value = data.link_host;
			if (!dirtyEnabled)
				battery.checked = b.enabled;
			if (!dirtyTarget)
				target.value = b.threshold;

			screenLabel.textContent = data.screen_active ? _("Current display: custom screen") :
				data.stock_active ? _("Current display: stock screen") : _("Switching screen services");
			var controller = batteryStates[b.control] || _("Unknown");
			if (b.control === 'held' && b.current_ma > 0)
				controller += _("; charging current is still detected");
			var rows = [
				row(_("Control status"), controller),
				row(_("Battery level"), b.gauge == null ? '—' : _("Gauge %s%% / GL interface approximately %s%%").format(b.gauge, b.display)),
				row(_("USB-C power"), b.online == null ? '—' : b.online ? _("Connected") : _("Disconnected")),
				row(_("Battery voltage"), measurement(b.voltage_mv, 'mV')),
				row(_("Battery current"), b.current_ma == null ? '—' : measurement(b.current_ma, 'mA') + ' ' + _("(positive: charging, negative: discharging, zero: battery inactive)")),
				row(_("Saved threshold"), _("%s%% (GL scale)").format(b.threshold) +
					(b.gauge_stop == null ? '' : _("; stops at approximately %s%% on the gauge and resumes below this level").format(b.gauge_stop)))
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
			feedback.textContent = _("Applying…");
			return Promise.resolve().then(action).then(function (data) {
				if (!data || !data.ok)
					throw new Error(rpcError(data, _("Operation failed.")));
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

		moduleItems.forEach(function (item) {
			moduleInputs[item[0]].addEventListener('change', function () {
				var desired = moduleItems.filter(function (entry) { return moduleInputs[entry[0]].checked; }).map(function (entry) { return entry[0]; });
				if (!desired.length) {
					moduleInputs[item[0]].checked = true;
					feedback.textContent = _("Select at least one screen module.");
					controls();
					return;
				}
				return apply(function () { return setScreenModules(desired); }, _("Screen modules saved."));
			});
		});
		screenLanguage.addEventListener('change', function () {
			var language = screenLanguage.value;
			return apply(function () { return setScreenLanguage(language); }, _("Custom screen language saved."));
		});
		screen.addEventListener('change', function () {
			var enabled = screen.checked;
			apply(function () { return setScreen(enabled); }, enabled ? _("Custom screen enabled.") : _("Switched to the stock screen."));
		});
		battery.addEventListener('change', function () { dirtyEnabled = true; });
		target.addEventListener('input', function () { dirtyTarget = true; });

		var applyStock = E('button', { 'class': 'btn cbi-button cbi-button-apply', type: 'button', disabled: true }, _("Apply Chinese localization"));
		applyStock.addEventListener('click', function () {
			return apply(function () { return setStockI18n('apply'); }, _("Stock UI Chinese localization applied."));
		});
		var restoreStock = E('button', { 'class': 'btn cbi-button cbi-button-reset', type: 'button', disabled: true }, _("Restore changes"));
		restoreStock.addEventListener('click', function () {
			return apply(function () { return setStockI18n('restore'); }, _("Stock UI resources restored from the backup."));
		});

		var save = E('button', { 'class': 'btn cbi-button cbi-button-apply', type: 'button', disabled: true }, _("Apply charge limit settings"));
		save.addEventListener('click', function () {
			var threshold = Number(target.value), enabled = battery.checked;
			if (!Number.isInteger(threshold) || threshold < 50 || threshold > 100) {
				feedback.textContent = _("Enter an integer from 50 to 100.");
				return;
			}
			apply(function () { return setBattery(enabled, threshold); }, _("Charge limit settings applied."), function () {
				dirtyEnabled = dirtyTarget = false;
			});
		});

		ruleGroup.addEventListener('input', function () { dirtyGroup = true; });
		var saveGroup = E('button', { 'class': 'btn cbi-button cbi-button-apply', type: 'button', disabled: true }, _("Save proxy group"));
		saveGroup.addEventListener('click', function () {
			var name = ruleGroup.value;
			if (!name.trim()) {
				feedback.textContent = _("Enter a proxy group name.");
				return;
			}
			apply(function () { return setRuleGroup(name); }, _("Proxy group saved. The touchscreen will read it on the next refresh."), function () {
				dirtyGroup = false;
			});
		});

		linkHost.addEventListener('input', function () { dirtyHost = true; });
		var saveHost = E('button', { 'class': 'btn cbi-button cbi-button-apply', type: 'button', disabled: true }, _("Save probe address"));
		saveHost.addEventListener('click', function () {
			var host = linkHost.value.trim();
			if (!host) {
				feedback.textContent = _("Enter an IPv4 / IPv6 address or domain name.");
				return;
			}
			if (host.length > 253) {
				feedback.textContent = _("Probe address is too long (maximum 253 characters).");
				return;
			}
			apply(function () { return setLinkHost(host); }, _("Probe address saved. The Monitor page will use it for the next probe (about 3 seconds)."), function () {
				dirtyHost = false;
			});
		});

		var clean = E('button', { 'class': 'btn cbi-button cbi-button-negative', type: 'button', disabled: true }, _("Clean up before uninstalling"));
		clean.addEventListener('click', function () {
			ui.showModal(_("Clean up before uninstalling"), [
				E('p', {}, _("This will switch to the stock screen, stop the custom screen, button switching and charge limit services, and delete brightness, PIN, charge limit settings, the link probe address and runtime data. You can then uninstall this plugin in the package manager.")),
				E('div', { 'class': 'right' }, [
					E('button', { 'class': 'btn', type: 'button', click: ui.hideModal }, _("Cancel")), ' ',
					E('button', {
						'class': 'btn cbi-button cbi-button-negative', type: 'button', click: function () {
							ui.hideModal();
							return apply(function () { return cleanup(true); }, _("Cleanup complete. You can now uninstall luci-mudi7-customizer."), function () {
								dirtyEnabled = dirtyTarget = dirtyGroup = dirtyHost = false;
							});
						}
					}, _("Confirm cleanup"))
				])
			]);
		});

		try { update(initial); }
		catch (error) { feedback.textContent = error.message; }
		poll.add(refresh, 5);

		return E('div', {}, [
			E('h2', {}, _("Mudi7 Customizer")),
			section(_("Stock UI Chinese support"), [
				E('p', {}, _("Matches the original language resources before applying and backs up all changed files. Unknown or changed entries stay unchanged. Layout fixes are applied only to verified screen resources. Restore changes returns files to their pre-localization state.")),
				stockState,
				stockWarning,
				E('p', {}, [applyStock, ' ', restoreStock])
			]),
			section(_("Custom screen"), [
				E('p', {}, [E('label', {}, [_("Interface language"), ' ', screenLanguage])]),
				E('p', {}, _("The custom screen switches language within about one second, without restarting. Proxy node and group names stay unchanged.")),
				E('label', {}, [screen, ' ', _("Enable custom screen")]),
				E('p', {}, [E('strong', {}, _("Visible screen modules"))]),
				moduleChoices,
				E('p', {}, _("Checked modules appear on the custom screen. Select at least one. Hiding a module does not disable its service. Changes take effect within about one second.")),
				E('p', {}, _("When enabled, switches to the custom screen and enables button switching between the stock and custom screens (hold Home for about 2 seconds). When disabled, the stock screen handles button input.")),
				screenLabel,
				E('hr'),
				E('p', {}, [E('strong', {}, _("OpenClash touchscreen settings"))]),
				E('p', {}, [E('label', {}, [_("Rule mode proxy group"), ' ', ruleGroup]), ' ', saveGroup]),
				E('p', {}, _("Enter the full name of the manual selection group in your Mihomo configuration. It is case-sensitive. The default is “默认代理”. Global mode uses GLOBAL.")),
				E('hr'),
				E('p', {}, [E('strong', {}, _("Monitor link probing"))]),
				E('p', {}, [E('label', {}, [_("Probe address"), ' ', linkHost]), ' ', saveHost]),
				E('p', {}, _("Link Health on the Monitor page sends ICMP probes to this address. Enter an IPv4 address (e.g. 1.1.1.1), an IPv6 address (e.g. 2400:3200::1), or a domain name (e.g. www.baidu.com, probed over IPv4). The default is www.baidu.com. Probing runs only while the Monitor page is visible and stops when you leave it. Changes take effect within about 3 seconds, without restarting the screen service."))
			]),
			section(_("Battery charge limit"), [
				E('p', {}, [E('label', {}, [battery, ' ', _("Enable battery charge limit")])]),
				E('p', {}, [E('label', {}, [_("Charge target (GL interface scale, 50–100%)"), ' ', target]), ' ', save]),
				E('p', {}, _("Settings are shared with the custom touchscreen. The battery charge limit can remain active after switching to the stock screen."))
			]),
			section(_("Battery status"), [live]),
			preserveSection(),
			section(_("Cleanup tools"), [clean]),
			feedback
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
