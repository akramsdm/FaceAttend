(function () {
  "use strict";

  function showError(message) {
    var box = document.getElementById("login-error");
    box.textContent = message;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("login-form");
    var username = document.getElementById("username");
    var password = document.getElementById("password");
    var submit = document.getElementById("login-submit");

    /* If a session is already active, skip the login form. */
    fetch("/api/admin/me", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (response) {
        if (response.ok) {
          window.location.replace(FA.safeNextPath());
        }
      })
      .catch(function () {
        /* Offline or API unavailable: keep the form usable. */
      });

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      showError("");

      if (!username.value.trim() || !password.value) {
        showError("Enter your username and password.");
        (username.value.trim() ? password : username).focus();
        return;
      }

      submit.disabled = true;
      submit.textContent = "Signing in...";

      try {
        var response = await fetch("/api/admin/login", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            username: username.value.trim(),
            password: password.value,
          }),
        });

        var payload = await response.json().catch(function () {
          return {};
        });

        if (response.status === 423) {
          showError(
            "This account is locked after repeated failed attempts. Try again later."
          );
          password.value = "";
          password.focus();
          return;
        }

        if (!response.ok) {
          var detail = payload.detail;
          if (typeof detail === "string") {
            showError(detail);
          } else if (response.status === 401) {
            showError("Invalid username or password.");
          } else {
            showError("Sign in failed. Please try again.");
          }
          password.value = "";
          password.focus();
          return;
        }

        FA.setCsrf(payload.csrf_token);
        window.location.replace(FA.safeNextPath());
      } catch (error) {
        showError("Cannot reach the server. Check your connection and try again.");
      } finally {
        submit.disabled = false;
        submit.textContent = "Sign in";
      }
    });
  });
})();
