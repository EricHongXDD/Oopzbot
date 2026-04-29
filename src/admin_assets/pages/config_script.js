    /* ---- Keyword editor helpers ---- */
    window._kwRender = function(obj) {
      var list = AdminShell.byId("cfg_kw_list");
      if (!list) return;
      list.innerHTML = "";
      Object.entries(obj).forEach(function(pair) { _kwAddRow(list, pair[0], pair[1]); });
    };
    function _kwAddRow(list, key, val) {
      var row = document.createElement("div");
      row.className = "kw-row";
      row.innerHTML =
        '<input class="kw-key" type="text" placeholder="关键词" value="' + _kwEsc(key) + '">' +
        '<input class="kw-val" type="text" placeholder="回复内容" value="' + _kwEsc(val) + '">' +
        '<button type="button" class="kw-del" title="删除">&times;</button>';
      row.querySelector(".kw-del").onclick = function() { row.remove(); };
      list.appendChild(row);
    }
    function _kwEsc(s) { return String(s || "").replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;"); }
    window._kwAdd = function() {
      var list = AdminShell.byId("cfg_kw_list");
      if (!list) return;
      _kwAddRow(list, "", "");
      var last = list.querySelector(".kw-row:last-child .kw-key");
      if (last) last.focus();
    };
    window._kwCollect = function() {
      var obj = {};
      var list = AdminShell.byId("cfg_kw_list");
      if (!list) return obj;
      list.querySelectorAll(".kw-row").forEach(function(row) {
        var k = row.querySelector(".kw-key").value.trim();
        var v = row.querySelector(".kw-val").value;
        if (k) obj[k] = v;
      });
      return obj;
    };

    function setVal(id, value) {
      const element = AdminShell.byId(id);
      if (!element) {
        return;
      }
      if (element.type === "checkbox") {
        element.checked = !!value;
      } else {
        element.value = value ?? "";
      }
    }

    function getInt(id) {
      const value = Number(AdminShell.byId(id).value);
      return Number.isFinite(value) ? Math.trunc(value) : 0;
    }

    function getFloat(id) {
      const value = Number(AdminShell.byId(id).value);
      return Number.isFinite(value) ? value : 0;
    }

    function val(id) {
      return (AdminShell.byId(id).value || "").trim();
    }

    function chk(id) {
      return !!AdminShell.byId(id).checked;
    }

    function setPageState(text, variant) {
      AdminShell.setStatus(text, variant, "topStatus");
      AdminShell.setStatus(text, variant, "mobileStatus");
      AdminShell.setStatus(text, variant, "railState");
    }

    function renderMusicAreaHint(info) {
      const hint = AdminShell.byId("cfg_oopz_area_hint");
      if (!hint) {
        return;
      }
      const area = info.area || "-";
      const sourceText = info.source_text || "未解析";
      const activeArea = info.active_area || "-";
      const defaultArea = info.default_area || "-";
      hint.textContent = "当前音乐域：" + area + "；来源：" + sourceText + "；活跃域：" + activeArea + "；默认域：" + defaultArea;
    }

    let neteaseQrTimer = null;
    let neteaseQrKey = "";
    let neteaseQrBaseUrl = "";

    function setNeteaseQrStatus(text, isError) {
      const element = AdminShell.byId("cfg_netease_qr_status");
      if (!element) {
        return;
      }
      element.textContent = text || "";
      element.style.color = isError ? "var(--danger)" : "var(--ink-faint)";
    }

    function renderNeteaseAccount(profile, message, isError) {
      const targets = Array.from(document.querySelectorAll("[data-netease-account-status]"));
      const legacy = AdminShell.byId("cfg_netease_account_status");
      if (legacy && !targets.includes(legacy)) {
        targets.push(legacy);
      }
      if (!targets.length) return;
      if (profile && profile.user_id) {
        const nickname = profile.nickname || "未命名账号";
        const text = "当前网易云账号：" + nickname + "（ID：" + profile.user_id + "）";
        targets.forEach(function (element) {
          element.textContent = text;
          element.style.color = "var(--success)";
        });
        return;
      }
      const text = "当前网易云账号：" + (message || "未登录");
      targets.forEach(function (element) {
        element.textContent = text;
        element.style.color = isError ? "var(--danger)" : "var(--ink-faint)";
      });
    }

    async function loadNeteaseAccountStatus(configured) {
      if (!configured) {
        renderNeteaseAccount(null, "未登录", false);
        return;
      }
      renderNeteaseAccount(null, "检测中...", false);
      try {
        const data = await AdminShell.req("/admin/api/netease/account");
        if (data.logged_in) {
          renderNeteaseAccount(data.profile || null, "", false);
        } else {
          renderNeteaseAccount(null, data.message || "未登录或 Cookie 已过期", false);
        }
      } catch (error) {
        renderNeteaseAccount(null, error.message || "账号状态检测失败", true);
      }
    }

    function setNeteaseQrSaveEnabled(enabled) {
      const persistButton = AdminShell.byId("cfg_netease_qr_save_persist");
      const runtimeButton = AdminShell.byId("cfg_netease_qr_save_runtime");
      if (persistButton) {
        persistButton.disabled = !enabled;
      }
      if (runtimeButton) {
        runtimeButton.disabled = !enabled;
      }
    }

    function stopNeteaseQrPolling() {
      if (neteaseQrTimer) {
        window.clearInterval(neteaseQrTimer);
        neteaseQrTimer = null;
      }
    }

    function resetNeteaseQrView(text) {
      const image = AdminShell.byId("cfg_netease_qr_img");
      const empty = AdminShell.byId("cfg_netease_qr_empty");
      const link = AdminShell.byId("cfg_netease_qr_link");
      if (image) {
        image.hidden = true;
        image.removeAttribute("src");
      }
      if (empty) {
        empty.hidden = false;
        empty.textContent = text || "等待刷新";
      }
      if (link) {
        link.hidden = true;
        link.href = "#";
      }
    }

    function fillNeteaseCookie(cookie) {
      setSecretValue("cfg_netease_cookie", cookie, true);
      setNeteaseQrSaveEnabled(!!cookie);
    }

    async function checkNeteaseQr() {
      if (!neteaseQrKey) {
        stopNeteaseQrPolling();
        return;
      }
      try {
        const data = await AdminShell.req("/admin/api/netease/login/qr/check", {
          method: "POST",
          body: JSON.stringify({
            base_url: neteaseQrBaseUrl || val("cfg_netease_base_url"),
            key: neteaseQrKey,
          }),
        });
        if (data.status === "waiting") {
          setNeteaseQrStatus(data.message || "等待扫码", false);
          return;
        }
        if (data.status === "scanned") {
          setNeteaseQrStatus(data.message || "已扫码，请在手机上确认", false);
          return;
        }
        if (data.status === "expired") {
          stopNeteaseQrPolling();
          neteaseQrKey = "";
          setNeteaseQrStatus(data.message || "二维码已过期", true);
          resetNeteaseQrView("已过期");
          setPageState("网易云二维码过期", "warning");
          return;
        }
        if (data.status === "success") {
          stopNeteaseQrPolling();
          neteaseQrKey = "";
          fillNeteaseCookie(data.cookie || "");
          renderNeteaseAccount(data.profile || null, data.profile_message || "已登录，账号信息待保存后刷新", false);
          setNeteaseQrStatus("登录成功，Cookie 已填入", false);
          AdminShell.showMessage("msg", "网易云 Cookie 已填入，可选择保存方式");
          setPageState("网易云登录成功", "success");
          return;
        }
        setNeteaseQrStatus(data.message || "等待网易云返回登录状态", false);
      } catch (error) {
        stopNeteaseQrPolling();
        neteaseQrKey = "";
        setNeteaseQrStatus(error.message || "登录状态检查失败", true);
        setPageState("网易云登录检查失败", "error");
      }
    }

    async function refreshNeteaseQr() {
      const button = AdminShell.byId("cfg_netease_qr_refresh");
      stopNeteaseQrPolling();
      neteaseQrKey = "";
      neteaseQrBaseUrl = "";
      setNeteaseQrSaveEnabled(false);
      resetNeteaseQrView("刷新中");
      setNeteaseQrStatus("正在获取二维码...", false);
      setPageState("网易云二维码刷新中", "warning");
      if (button) {
        button.disabled = true;
        button.textContent = "刷新中...";
      }
      try {
        const data = await AdminShell.req("/admin/api/netease/login/qr", {
          method: "POST",
          body: JSON.stringify({ base_url: val("cfg_netease_base_url") }),
        });
        neteaseQrKey = data.key || "";
        neteaseQrBaseUrl = data.base_url || val("cfg_netease_base_url");
        const image = AdminShell.byId("cfg_netease_qr_img");
        const empty = AdminShell.byId("cfg_netease_qr_empty");
        const link = AdminShell.byId("cfg_netease_qr_link");
        if (image && data.qrimg) {
          image.src = data.qrimg;
          image.hidden = false;
        }
        if (empty) {
          empty.hidden = !!data.qrimg;
          empty.textContent = data.qrimg ? "" : "二维码链接已生成";
        }
        if (link && data.qrurl) {
          link.href = data.qrurl;
          link.hidden = false;
        }
        setNeteaseQrStatus(data.message || "等待扫码", false);
        neteaseQrTimer = window.setInterval(checkNeteaseQr, 2500);
        window.setTimeout(checkNeteaseQr, 900);
      } catch (error) {
        resetNeteaseQrView("刷新失败");
        setNeteaseQrStatus(error.message || "二维码刷新失败", true);
        setPageState("网易云二维码刷新失败", "error");
      } finally {
        if (button) {
          button.disabled = false;
          button.textContent = "刷新二维码";
        }
      }
    }

    async function saveNeteaseQrCookie(persist) {
      if (!val("cfg_netease_cookie")) {
        setNeteaseQrStatus("请先完成扫码登录", true);
        return;
      }
      await saveConfig(!!persist);
    }

    let bilibiliQrTimer = null;
    let bilibiliQrKey = "";

    function setBilibiliQrStatus(text, isError) {
      const element = AdminShell.byId("cfg_bilibili_qr_status");
      if (!element) {
        return;
      }
      element.textContent = text || "";
      element.style.color = isError ? "var(--danger)" : "var(--ink-faint)";
    }

    function renderBilibiliAccount(profile, message, isError) {
      const targets = Array.from(document.querySelectorAll("[data-bilibili-account-status]"));
      const legacy = AdminShell.byId("cfg_bilibili_account_status");
      if (legacy && !targets.includes(legacy)) {
        targets.push(legacy);
      }
      let text = "";
      if (!targets.length) {
        if (profile && profile.user_id) {
          const nickname = profile.nickname || "未命名账号";
          setBilibiliQrStatus("当前B站账号：" + nickname + "（ID：" + profile.user_id + "）", false);
        } else if (message) {
          setBilibiliQrStatus("当前B站账号：" + message, !!isError);
        }
        return;
      }
      if (profile && profile.user_id) {
        const nickname = profile.nickname || "未命名账号";
        text = "当前B站账号：" + nickname + "（ID：" + profile.user_id + "）";
        targets.forEach(function (element) {
          element.textContent = text;
          element.style.color = "var(--success)";
        });
        return;
      }
      text = "当前B站账号：" + (message || "未登录");
      targets.forEach(function (element) {
        element.textContent = text;
        element.style.color = isError ? "var(--danger)" : "var(--ink-faint)";
      });
    }

    function bilibiliAccountText(profile, fallback) {
      if (profile && profile.user_id) {
        const nickname = profile.nickname || "未命名账号";
        return "当前B站账号：" + nickname + "（ID：" + profile.user_id + "）";
      }
      return fallback || "当前B站账号：已登录，账号信息待保存后刷新";
    }

    async function loadBilibiliAccountStatus(configured) {
      if (!configured) {
        renderBilibiliAccount(null, "未登录", false);
        return;
      }
      renderBilibiliAccount(null, "检测中...", false);
      try {
        const data = await AdminShell.req("/admin/api/bilibili/account");
        if (data.logged_in) {
          renderBilibiliAccount(data.profile || null, "", false);
        } else {
          renderBilibiliAccount(null, data.message || "未登录或 Cookie 已过期", false);
        }
      } catch (error) {
        renderBilibiliAccount(null, error.message || "账号状态检测失败", true);
      }
    }

    function setBilibiliQrSaveEnabled(enabled) {
      const persistButton = AdminShell.byId("cfg_bilibili_qr_save_persist");
      const runtimeButton = AdminShell.byId("cfg_bilibili_qr_save_runtime");
      if (persistButton) {
        persistButton.disabled = !enabled;
      }
      if (runtimeButton) {
        runtimeButton.disabled = !enabled;
      }
    }

    function stopBilibiliQrPolling() {
      if (bilibiliQrTimer) {
        window.clearInterval(bilibiliQrTimer);
        bilibiliQrTimer = null;
      }
    }

    function resetBilibiliQrView(text) {
      const image = AdminShell.byId("cfg_bilibili_qr_img");
      const empty = AdminShell.byId("cfg_bilibili_qr_empty");
      const link = AdminShell.byId("cfg_bilibili_qr_link");
      if (image) {
        image.hidden = true;
        image.removeAttribute("src");
      }
      if (empty) {
        empty.hidden = false;
        empty.textContent = text || "等待刷新";
      }
      if (link) {
        link.hidden = true;
        link.href = "#";
      }
    }

    function fillBilibiliCookie(cookie) {
      setSecretValue("cfg_bilibili_cookie", cookie, true);
      setBilibiliQrSaveEnabled(!!cookie);
    }

    async function checkBilibiliQr() {
      if (!bilibiliQrKey) {
        stopBilibiliQrPolling();
        return;
      }
      try {
        const data = await AdminShell.req("/admin/api/bilibili/login/qr/check", {
          method: "POST",
          body: JSON.stringify({ key: bilibiliQrKey }),
        });
        if (data.status === "waiting") {
          setBilibiliQrStatus(data.message || "等待扫码", false);
          return;
        }
        if (data.status === "scanned") {
          setBilibiliQrStatus(data.message || "已扫码，请在手机上确认", false);
          return;
        }
        if (data.status === "expired") {
          stopBilibiliQrPolling();
          bilibiliQrKey = "";
          setBilibiliQrStatus(data.message || "二维码已过期", true);
          resetBilibiliQrView("已过期");
          setPageState("B 站二维码过期", "warning");
          return;
        }
        if (data.status === "success") {
          stopBilibiliQrPolling();
          bilibiliQrKey = "";
          fillBilibiliCookie(data.cookie || "");
          const accountText = bilibiliAccountText(data.profile || null, data.profile_message || "");
          renderBilibiliAccount(data.profile || null, data.profile_message || "已登录，账号信息待保存后刷新", false);
          setBilibiliQrStatus("登录成功，Cookie 已填入", false);
          AdminShell.showMessage("msg", "B 站 Cookie 已填入，可选择保存方式；" + accountText);
          setPageState("B 站登录成功", "success");
          return;
        }
        setBilibiliQrStatus(data.message || "等待 B 站返回登录状态", false);
      } catch (error) {
        stopBilibiliQrPolling();
        bilibiliQrKey = "";
        setBilibiliQrStatus(error.message || "登录状态检查失败", true);
        setPageState("B 站登录检查失败", "error");
      }
    }

    async function refreshBilibiliQr() {
      const button = AdminShell.byId("cfg_bilibili_qr_refresh");
      stopBilibiliQrPolling();
      bilibiliQrKey = "";
      setBilibiliQrSaveEnabled(false);
      resetBilibiliQrView("刷新中");
      setBilibiliQrStatus("正在获取二维码...", false);
      setPageState("B 站二维码刷新中", "warning");
      if (button) {
        button.disabled = true;
        button.textContent = "刷新中...";
      }
      try {
        const data = await AdminShell.req("/admin/api/bilibili/login/qr", {
          method: "POST",
          body: "{}",
        });
        bilibiliQrKey = data.key || "";
        const image = AdminShell.byId("cfg_bilibili_qr_img");
        const empty = AdminShell.byId("cfg_bilibili_qr_empty");
        const link = AdminShell.byId("cfg_bilibili_qr_link");
        if (image && data.qrimg) {
          image.src = data.qrimg;
          image.hidden = false;
        }
        if (empty) {
          empty.hidden = !!data.qrimg;
          empty.textContent = data.qrimg ? "" : "二维码链接已生成";
        }
        if (link && data.qrurl) {
          link.href = data.qrurl;
          link.hidden = false;
        }
        setBilibiliQrStatus(data.message || "等待扫码", false);
        bilibiliQrTimer = window.setInterval(checkBilibiliQr, 2500);
        window.setTimeout(checkBilibiliQr, 900);
      } catch (error) {
        resetBilibiliQrView("刷新失败");
        setBilibiliQrStatus(error.message || "二维码刷新失败", true);
        setPageState("B 站二维码刷新失败", "error");
      } finally {
        if (button) {
          button.disabled = false;
          button.textContent = "刷新二维码";
        }
      }
    }

    async function saveBilibiliQrCookie(persist) {
      if (!val("cfg_bilibili_cookie")) {
        setBilibiliQrStatus("请先完成扫码登录", true);
        return;
      }
      await saveConfig(!!persist);
    }

    function setSecretState(id, configured) {
      const element = AdminShell.byId(id);
      if (!element) {
        return;
      }
      element.type = "password";
      element.value = "";
      element.placeholder = configured ? "已配置，留空表示不修改" : "未配置";
      const button = element.parentElement && element.parentElement.querySelector("button");
      if (button) {
        button.textContent = "显示";
      }
    }

    function setSecretValue(id, value, configured) {
      const element = AdminShell.byId(id);
      if (!element) {
        return;
      }
      element.type = "password";
      element.value = value || "";
      element.placeholder = configured && !value ? "已配置，留空表示不修改" : "";
      const button = element.parentElement && element.parentElement.querySelector("button");
      if (button) {
        button.textContent = "显示";
      }
    }

    function toggleSecret(id, button) {
      const element = AdminShell.byId(id);
      if (!element) {
        return;
      }
      const show = element.type === "password";
      element.type = show ? "text" : "password";
      if (button) {
        button.textContent = show ? "隐藏" : "显示";
      }
    }

    async function check() {
      try {
        await AdminShell.req("/admin/api/me");
        AdminShell.setAuthState({
          loggedIn: true,
          loggedInText: "已登录配置页",
          statusTargets: ["topStatus", "mobileStatus"],
        });
        await loadConfig();
      } catch (_) {
        AdminShell.setAuthState({
          loggedIn: false,
          loggedOutText: "等待登录",
          statusTargets: ["topStatus", "mobileStatus"],
        });
        AdminShell.showMessage("loginMsg", "");
        setPageState("等待操作", "warning");
      }
    }

    async function login() {
      try {
        await AdminShell.req("/admin/api/login", {
          method: "POST",
          body: JSON.stringify({ password: AdminShell.byId("pwd").value || "" }),
        });
        AdminShell.showMessage("loginMsg", "登录成功");
        await check();
      } catch (error) {
        AdminShell.showMessage("loginMsg", error.message, true);
        setPageState("登录失败", "error");
      }
    }

    async function logout() {
      try {
        await AdminShell.req("/admin/api/logout", { method: "POST", body: "{}" });
      } catch (_) {
      }
      await check();
    }

    function setOopzLoginStatus(text, variant) {
      const element = AdminShell.byId("cfg_oopz_login_status");
      if (!element) {
        return;
      }
      const state = variant || "neutral";
      element.textContent = text;
      element.classList.toggle("is-error", state === "error");
      element.classList.toggle("is-success", state === "success");
    }

    function setOopzLoginButtonLoading(loading) {
      const button = AdminShell.byId("cfg_oopz_login_btn");
      if (!button) {
        return;
      }
      button.disabled = !!loading;
      button.textContent = loading ? "登录中..." : "登录并获取";
    }

    function formatOopzCredentialSummary(credentials) {
      const c = credentials || {};
      const expires = c.expires_at ? "；Token 到期：" + c.expires_at : "";
      return "已获取 UID " + (c.person_uid || "-") + "，设备 " + (c.device_id || "-") + expires;
    }

    async function loginOopzAccount() {
      const phone = val("cfg_oopz_login_phone");
      const passwordElement = AdminShell.byId("cfg_oopz_login_password");
      const password = passwordElement ? passwordElement.value : "";
      if (!phone || !password) {
        setOopzLoginStatus("请输入 OOPZ 账号和密码", "error");
        return;
      }

      setOopzLoginButtonLoading(true);
      setOopzLoginStatus("正在登录 OOPZ...", "neutral");
      setPageState("OOPZ 登录中", "warning");

      try {
        const data = await AdminShell.req("/admin/api/oopz/login", {
          method: "POST",
          body: JSON.stringify({ phone, password }),
        });
        if (passwordElement) {
          passwordElement.value = "";
        }
        const saved = Array.isArray(data.saved) && data.saved.length > 0 ? data.saved.join("、") : "运行时";
        setOopzLoginStatus(formatOopzCredentialSummary(data.credentials), "success");
        AdminShell.showMessage("msg", "OOPZ 凭据已保存到 " + saved + "，建议重启后完整生效");
        setPageState("OOPZ 登录成功", "success");
        await loadConfig();
      } catch (error) {
        setOopzLoginStatus(error.message || "OOPZ 登录失败", "error");
        AdminShell.showMessage("msg", error.message || "OOPZ 登录失败", true);
        setPageState("OOPZ 登录失败", "error");
      } finally {
        setOopzLoginButtonLoading(false);
      }
    }

    async function loadConfig() {
      const data = await AdminShell.req("/admin/api/config");
      const config = data.config || {};
      const runtime = data.runtime || {};

      setVal("cfg_web_url", config.web_player?.url || "");
      setVal("cfg_web_host", config.web_player?.host || "0.0.0.0");
      setVal("cfg_web_port", config.web_player?.port || 8080);
      setVal("cfg_web_token_ttl", config.web_player?.token_ttl_seconds || 0);
      setVal("cfg_web_cookie_age", config.web_player?.cookie_max_age_seconds || 0);
      setVal("cfg_link_idle", config.web_player?.link_idle_release_seconds || 0);
      setVal("cfg_cookie_secure", config.web_player?.cookie_secure);
      setVal("cfg_admin_session_ttl", config.web_player?.admin_session_ttl_seconds || 0);
      setVal("cfg_admin_cookie_secure", config.web_player?.admin_cookie_secure);

      setVal("cfg_auto_recall_enabled", config.auto_recall?.enabled);
      setVal("cfg_auto_recall_delay", config.auto_recall?.delay || 30);
      setVal("cfg_auto_recall_exclude", (config.auto_recall?.exclude_commands || []).join(", "));
      setVal("cfg_area_notify_enabled", config.area_join_notify?.enabled);
      setVal("cfg_area_poll", config.area_join_notify?.poll_interval_seconds || 2);
      setVal("cfg_area_auto_role_id", config.area_join_notify?.auto_assign_role_id || "");
      setVal("cfg_area_auto_role_name", config.area_join_notify?.auto_assign_role_name || "");
      setVal("cfg_chat_enabled", config.chat?.enabled);
      window._kwRender(config.chat?.keyword_replies || {});
      setVal("cfg_profanity_enabled", config.profanity?.enabled);
      setVal("cfg_profanity_recall", config.profanity?.recall_message);
      setVal("cfg_mute_duration", config.profanity?.mute_duration || 5);
      setVal("cfg_warn_before_mute", config.profanity?.warn_before_mute);
      setVal("cfg_skip_admins", config.profanity?.skip_admins);
      setVal("cfg_context_detection", config.profanity?.context_detection);
      setVal("cfg_context_window", config.profanity?.context_window || 30);
      setVal("cfg_context_max_messages", config.profanity?.context_max_messages || 10);
      setVal("cfg_ai_detection", config.profanity?.ai_detection);
      setVal("cfg_ai_min_length", config.profanity?.ai_min_length || 2);

      setVal("cfg_oopz_default_area", config.oopz?.default_area || "");
      setVal("cfg_oopz_default_channel", config.oopz?.default_channel || "");
      setVal("cfg_oopz_proxy", config.oopz?.proxy || "");
      setVal("cfg_oopz_announcement", config.oopz?.use_announcement_style);
      setVal("cfg_agora_app_id", config.oopz?.agora_app_id || "");
      setVal("cfg_agora_timeout", config.oopz?.agora_init_timeout || 1800);

      setVal("cfg_netease_base_url", config.netease?.base_url || "");
      setVal("cfg_netease_timeout", config.netease?.audio_download_timeout || 120);
      setVal("cfg_netease_retries", config.netease?.audio_download_retries || 2);
      setVal("cfg_netease_quality", config.netease?.audio_quality || "standard");

      setVal("cfg_redis_host", config.redis?.host || "");
      setVal("cfg_redis_port", config.redis?.port || 6379);
      setVal("cfg_redis_db", config.redis?.db || 0);
      setVal("cfg_redis_decode", config.redis?.decode_responses);

      setVal("cfg_doubao_enabled", config.doubao_chat?.enabled);
      setVal("cfg_doubao_base_url", config.doubao_chat?.base_url || "");
      setVal("cfg_doubao_model", config.doubao_chat?.model || "");
      setVal("cfg_doubao_max_tokens", config.doubao_chat?.max_tokens || 256);
      setVal("cfg_doubao_temperature", config.doubao_chat?.temperature ?? 0.7);
      setVal("cfg_doubao_system_prompt", config.doubao_chat?.system_prompt || "");

      setVal("cfg_doubao_context_rounds", config.doubao_chat?.context_max_rounds ?? 0);
      setVal("cfg_doubao_context_ttl", config.doubao_chat?.context_ttl_seconds ?? 0);

      setVal("cfg_doubao_img_enabled", config.doubao_image?.enabled);
      setVal("cfg_doubao_img_base_url", config.doubao_image?.base_url || "");
      setVal("cfg_doubao_img_model", config.doubao_image?.model || "");
      setVal("cfg_doubao_img_size", config.doubao_image?.size || "");
      setVal("cfg_doubao_img_watermark", config.doubao_image?.watermark);

      setVal("cfg_music_auto_play", config.music?.auto_play_enabled);
      setVal("cfg_music_volume", config.music?.default_volume ?? 50);

      setVal("cfg_qq_music_enabled", config.qq_music?.enabled);
      setVal("cfg_qq_music_base_url", config.qq_music?.base_url || "");

      setVal("cfg_bilibili_enabled", config.bilibili_music?.enabled);

      setVal("cfg_stats_enabled", config.message_stats?.enabled);

      setVal("cfg_cooldown_enabled", config.command_cooldown?.enabled);
      setVal("cfg_cooldown_seconds", config.command_cooldown?.default_seconds ?? 3);
      setVal("cfg_cooldown_exempt_admins", config.command_cooldown?.exempt_admins);

      setVal("cfg_scheduler_enabled", config.scheduler?.enabled);
      setVal("cfg_scheduler_interval", config.scheduler?.check_interval_seconds ?? 30);
      setVal("cfg_reminder_enabled", config.reminder?.enabled);
      setVal("cfg_reminder_max_per_user", config.reminder?.max_per_user ?? 5);
      setVal("cfg_reminder_max_delay", config.reminder?.max_delay_hours ?? 72);
      setVal("cfg_reminder_interval", config.reminder?.check_interval_seconds ?? 15);

      setVal("cfg_notify_join_tpl", config.area_join_notify?.message_template || "");
      setVal("cfg_notify_leave_tpl", config.area_join_notify?.message_template_leave || "");

      setSecretState("cfg_admin_password", config.web_player?.admin_password_configured);
      setSecretValue("cfg_netease_cookie", config.netease?.cookie || "", config.netease?.cookie_configured);
      setSecretState("cfg_redis_password", config.redis?.password_configured);
      setSecretValue("cfg_doubao_api_key", config.doubao_chat?.api_key || "", config.doubao_chat?.api_key_configured);
      setSecretValue("cfg_doubao_img_api_key", config.doubao_image?.api_key || "", config.doubao_image?.api_key_configured);
      setSecretValue("cfg_qq_music_cookie", config.qq_music?.cookie || "", config.qq_music?.cookie_configured);
      setSecretValue("cfg_bilibili_cookie", config.bilibili_music?.cookie || "", config.bilibili_music?.cookie_configured);
      setNeteaseQrSaveEnabled(false);
      setBilibiliQrSaveEnabled(false);
      renderMusicAreaHint(runtime.music_area || {});
      await loadNeteaseAccountStatus(!!config.netease?.cookie_configured);
      await loadBilibiliAccountStatus(!!config.bilibili_music?.cookie_configured);

      setPageState("配置已同步", "success");
    }

    function build() {
      var keywordsObj = window._kwCollect();

      const updates = {
        web_player: {
          url: val("cfg_web_url"),
          host: val("cfg_web_host") || "0.0.0.0",
          port: getInt("cfg_web_port") || 8080,
          token_ttl_seconds: getInt("cfg_web_token_ttl"),
          cookie_max_age_seconds: getInt("cfg_web_cookie_age"),
          link_idle_release_seconds: getInt("cfg_link_idle"),
          cookie_secure: chk("cfg_cookie_secure"),
          admin_session_ttl_seconds: getInt("cfg_admin_session_ttl"),
          admin_cookie_secure: chk("cfg_admin_cookie_secure"),
        },
        auto_recall: {
          enabled: chk("cfg_auto_recall_enabled"),
          delay: getInt("cfg_auto_recall_delay"),
          exclude_commands: val("cfg_auto_recall_exclude"),
        },
        area_join_notify: {
          enabled: chk("cfg_area_notify_enabled"),
          poll_interval_seconds: getInt("cfg_area_poll"),
          message_template: val("cfg_notify_join_tpl"),
          message_template_leave: val("cfg_notify_leave_tpl"),
          auto_assign_role_id: val("cfg_area_auto_role_id"),
          auto_assign_role_name: val("cfg_area_auto_role_name"),
        },
        chat: {
          enabled: chk("cfg_chat_enabled"),
          keyword_replies: keywordsObj,
        },
        profanity: {
          enabled: chk("cfg_profanity_enabled"),
          recall_message: chk("cfg_profanity_recall"),
          mute_duration: getInt("cfg_mute_duration"),
          warn_before_mute: chk("cfg_warn_before_mute"),
          skip_admins: chk("cfg_skip_admins"),
          context_detection: chk("cfg_context_detection"),
          context_window: getInt("cfg_context_window"),
          context_max_messages: getInt("cfg_context_max_messages"),
          ai_detection: chk("cfg_ai_detection"),
          ai_min_length: getInt("cfg_ai_min_length"),
        },
        oopz: {
          default_area: val("cfg_oopz_default_area"),
          default_channel: val("cfg_oopz_default_channel"),
          proxy: val("cfg_oopz_proxy"),
          use_announcement_style: chk("cfg_oopz_announcement"),
          agora_app_id: val("cfg_agora_app_id"),
          agora_init_timeout: getInt("cfg_agora_timeout") || 1800,
        },
        netease: {
          base_url: val("cfg_netease_base_url"),
          audio_download_timeout: getInt("cfg_netease_timeout"),
          audio_download_retries: getInt("cfg_netease_retries"),
          audio_quality: val("cfg_netease_quality") || "standard",
        },
        redis: {
          host: val("cfg_redis_host"),
          port: getInt("cfg_redis_port"),
          db: getInt("cfg_redis_db"),
          decode_responses: chk("cfg_redis_decode"),
        },
        doubao_chat: {
          enabled: chk("cfg_doubao_enabled"),
          base_url: val("cfg_doubao_base_url"),
          model: val("cfg_doubao_model"),
          max_tokens: getInt("cfg_doubao_max_tokens"),
          temperature: getFloat("cfg_doubao_temperature"),
          system_prompt: val("cfg_doubao_system_prompt"),
          context_max_rounds: getInt("cfg_doubao_context_rounds"),
          context_ttl_seconds: getInt("cfg_doubao_context_ttl"),
        },
        doubao_image: {
          enabled: chk("cfg_doubao_img_enabled"),
          base_url: val("cfg_doubao_img_base_url"),
          model: val("cfg_doubao_img_model"),
          size: val("cfg_doubao_img_size"),
          watermark: chk("cfg_doubao_img_watermark"),
        },
        music: {
          auto_play_enabled: chk("cfg_music_auto_play"),
          default_volume: getInt("cfg_music_volume"),
        },
        command_cooldown: {
          enabled: chk("cfg_cooldown_enabled"),
          default_seconds: getInt("cfg_cooldown_seconds"),
          exempt_admins: chk("cfg_cooldown_exempt_admins"),
        },
        qq_music: {
          enabled: chk("cfg_qq_music_enabled"),
          base_url: val("cfg_qq_music_base_url"),
        },
        bilibili_music: {
          enabled: chk("cfg_bilibili_enabled"),
        },
        message_stats: {
          enabled: chk("cfg_stats_enabled"),
        },
        scheduler: {
          enabled: chk("cfg_scheduler_enabled"),
          check_interval_seconds: getInt("cfg_scheduler_interval"),
        },
        reminder: {
          enabled: chk("cfg_reminder_enabled"),
          max_per_user: getInt("cfg_reminder_max_per_user"),
          max_delay_hours: getInt("cfg_reminder_max_delay"),
          check_interval_seconds: getInt("cfg_reminder_interval"),
        },
      };

      const adminPassword = val("cfg_admin_password");
      const neteaseCookie = val("cfg_netease_cookie");
      const redisPassword = val("cfg_redis_password");
      const doubaoApiKey = val("cfg_doubao_api_key");
      const doubaoImageApiKey = val("cfg_doubao_img_api_key");
      const qqMusicCookie = val("cfg_qq_music_cookie");
      const bilibiliCookie = val("cfg_bilibili_cookie");

      if (adminPassword) {
        updates.web_player.admin_password = adminPassword;
      }
      if (neteaseCookie) {
        updates.netease.cookie = neteaseCookie;
      }
      if (redisPassword) {
        updates.redis.password = redisPassword;
      }
      if (doubaoApiKey) {
        updates.doubao_chat.api_key = doubaoApiKey;
      }
      if (doubaoImageApiKey) {
        updates.doubao_image.api_key = doubaoImageApiKey;
      }
      if (qqMusicCookie) {
        updates.qq_music.cookie = qqMusicCookie;
      }
      if (bilibiliCookie) {
        updates.bilibili_music.cookie = bilibiliCookie;
      }

      return updates;
    }

    async function saveConfig(persist) {
      try {
        const data = await AdminShell.req("/admin/api/config", {
          method: "POST",
          body: JSON.stringify({ updates: build(), persist }),
        });
        if (Array.isArray(data.errors) && data.errors.length > 0) {
          AdminShell.showMessage("msg", "部分失败：" + data.errors.join(" | "), true);
          setPageState("配置保存有错误", "error");
        } else {
          AdminShell.showMessage("msg", persist ? "配置已保存并持久化" : "配置已更新，仅当前进程生效");
          setPageState("配置已保存", "success");
        }
        await loadConfig();
      } catch (error) {
        AdminShell.showMessage("msg", error.message, true);
        setPageState("配置保存失败", "error");
      }
    }

    async function resetOverrides() {
      try {
        await AdminShell.req("/admin/api/config/reset", { method: "POST", body: "{}" });
        AdminShell.showMessage("msg", "已清除持久化覆盖，重启后回退到 config.py");
        setPageState("覆盖已清除", "warning");
        await loadConfig();
      } catch (error) {
        AdminShell.showMessage("msg", error.message, true);
      }
    }

    function initTabs() {
      var tabs = document.querySelectorAll(".cfg-tab");
      var panels = document.querySelectorAll(".cfg-panel");
      tabs.forEach(function (tab) {
        tab.addEventListener("click", function () {
          var key = tab.dataset.tab;
          tabs.forEach(function (t) { t.classList.toggle("is-active", t.dataset.tab === key); });
          panels.forEach(function (p) { p.classList.toggle("is-active", p.dataset.panel === key); });
        });
      });
    }

    AdminShell.init({ page: "config", passwordHandler: login });
    initTabs();
    check();
