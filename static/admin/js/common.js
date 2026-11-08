/* Shared admin helpers: session, CSRF and API access. */
(function () {
  "use strict";

  var CSRF_KEY = "fa_csrf";
  var LOGIN_PATH = "/admin/login.html";

  function getCsrf() {
    try {
      return sessionStorage.getItem(CSRF_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function setCsrf(token) {
    if (!token) {
      return;
    }
    try {
      sessionStorage.setItem(CSRF_KEY, token);
    } catch (error) {
      /* Storage can be unavailable in locked-down browsers. */
    }
  }

  function clearCsrf() {
    try {
      sessionStorage.removeItem(CSRF_KEY);
    } catch (error) {
      /* Ignore. */
    }
  }

  function loginUrl() {
    var here = window.location.pathname + window.location.search;
    return LOGIN_PATH + "?next=" + encodeURIComponent(here);
  }

  function redirectToLogin() {
    window.location.replace(loginUrl());
  }

  async function request(path, options) {
    var opts = Object.assign({ credentials: "same-origin" }, options || {});
    var headers = new Headers(opts.headers || {});
    headers.set("Accept", "application/json");

    var method = (opts.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      var token = getCsrf();
      if (token) {
        headers.set("X-CSRF-Token", token);
      }
    }
    opts.headers = headers;

    var response = await fetch(path, opts);
    if (response.status === 401 || response.status === 403) {
      clearCsrf();
      redirectToLogin();
      throw new Error("Your session has ended. Redirecting to sign in.");
    }
    return response;
  }

  async function readError(response) {
    try {
      var payload = await response.json();
      var detail = payload.detail;
      if (typeof detail === "string") {
        return detail;
      }
      if (Array.isArray(detail)) {
        return detail
          .map(function (item) {
            return item.msg || "Invalid value";
          })
          .join("; ");
      }
      if (detail && typeof detail === "object") {
        return detail.message || "The request was rejected.";
      }
    } catch (error) {
      /* Response was not JSON. */
    }
    return "Request failed (" + response.status + ").";
  }

  async function requireSession() {
    var response = await request("/api/admin/me");
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    var payload = await response.json();
    setCsrf(payload.csrf_token);
    return payload;
  }

  function showAdminName(name) {
    var nodes = document.querySelectorAll("[data-admin-name]");
    for (var index = 0; index < nodes.length; index += 1) {
      nodes[index].textContent = name;
    }
  }

  function wireLogout() {
    var button = document.querySelector("[data-logout]");
    if (!button) {
      return;
    }
    button.addEventListener("click", async function () {
      button.disabled = true;
      try {
        var response = await request("/api/admin/logout", { method: "POST" });
        if (!response.ok) {
          throw new Error(await readError(response));
        }
        clearCsrf();
        window.location.replace(LOGIN_PATH);
      } catch (error) {
        button.disabled = false;
        if (error && error.message && error.message.indexOf("session") !== -1) {
          return;
        }
        window.alert("Sign out failed. Please try again.");
      }
    });
  }

  function startSessionWatch() {
    window.setInterval(async function () {
      if (document.hidden) {
        return;
      }
      try {
        await request("/api/admin/me");
      } catch (error) {
        /* request() already redirects expired sessions. */
      }
    }, 60000);
  }

  function safeNextPath() {
    var raw = new URLSearchParams(window.location.search).get("next");
    if (!raw) {
      return "/admin/dashboard.html";
    }
    try {
      var url = new URL(raw, window.location.origin);
      if (
        url.origin === window.location.origin &&
        url.pathname.indexOf("/admin/") === 0 &&
        url.pathname !== LOGIN_PATH
      ) {
        return url.pathname + url.search;
      }
    } catch (error) {
      /* Fall through to the default. */
    }
    return "/admin/dashboard.html";
  }

  window.FA = {
    getCsrf: getCsrf,
    setCsrf: setCsrf,
    clearCsrf: clearCsrf,
    request: request,
    readError: readError,
    requireSession: requireSession,
    showAdminName: showAdminName,
    wireLogout: wireLogout,
    startSessionWatch: startSessionWatch,
    redirectToLogin: redirectToLogin,
    safeNextPath: safeNextPath,
    loginPath: LOGIN_PATH,
  };
})();
