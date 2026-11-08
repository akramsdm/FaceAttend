(function () {
  "use strict";

  var rows = null;
  var countLabel = null;
  var messageBox = null;
  var editor = null;
  var editorTitle = null;
  var editorError = null;
  var form = null;
  var idInput = null;
  var codeInput = null;
  var nameInput = null;
  var departmentInput = null;
  var shiftInput = null;
  var activeField = null;
  var activeInput = null;
  var saveButton = null;
  var queryInput = null;
  var activeFilter = null;
  var loadSequence = 0;
  var saving = false;

  function byId(id) {
    return document.getElementById(id);
  }

  function setMessage(text, kind) {
    messageBox.textContent = text || "";
    messageBox.className = "message" + (kind ? " " + kind : "");
  }

  function setEditorError(text) {
    editorError.textContent = text || "";
  }

  function cell(text, className) {
    var element = document.createElement("td");
    element.textContent = text;
    if (className) {
      element.className = className;
    }
    return element;
  }

  function statusCell(active) {
    var td = document.createElement("td");
    var badge = document.createElement("span");
    badge.className = "status " + (active ? "status-active" : "status-inactive");
    badge.textContent = active ? "Active" : "Inactive";
    td.appendChild(badge);
    return td;
  }

  function button(label, className, onClick, ariaLabel) {
    var element = document.createElement("button");
    element.type = "button";
    element.className = className;
    element.textContent = label;
    if (ariaLabel) {
      element.setAttribute("aria-label", ariaLabel);
    }
    element.addEventListener("click", onClick);
    return element;
  }

  function buildRow(person) {
    var row = document.createElement("tr");
    row.appendChild(cell(person.staff_code));
    row.appendChild(cell(person.name));
    row.appendChild(
      cell(
        person.department || "Not set",
        person.department ? "" : "muted"
      )
    );
    row.appendChild(cell(person.shift || "Not set", person.shift ? "" : "muted"));
    row.appendChild(statusCell(person.active));
    row.appendChild(
      cell(
        person.enrolled
          ? "Enrolled (" + person.embedding_count + " embeddings)"
          : "Not enrolled",
        person.enrolled ? "" : "muted"
      )
    );

    var actions = document.createElement("td");
    var group = document.createElement("div");
    group.className = "row-actions";

    group.appendChild(
      button(
        person.enrolled ? "Re-enrol faces" : "Enrol faces",
        "button ghost",
        function () {
          window.location.href =
            "enrol.html?staff_id=" + encodeURIComponent(person.id);
        },
        (person.enrolled ? "Re-enrol faces for " : "Enrol faces for ") +
          person.name
      )
    );

    group.appendChild(
      button("Edit", "button ghost", function () {
        openEditor(person);
      }, "Edit " + person.name)
    );

    if (person.active) {
      group.appendChild(
        button("Deactivate", "button ghost", function (event) {
          setActive(person, false, event.currentTarget);
        }, "Deactivate " + person.name)
      );
    } else {
      group.appendChild(
        button("Activate", "button ghost", function (event) {
          setActive(person, true, event.currentTarget);
        }, "Activate " + person.name)
      );
    }

    group.appendChild(
      button("Delete", "button ghost", function (event) {
        removeStaff(person, event.currentTarget);
      }, "Delete " + person.name)
    );

    actions.appendChild(group);
    row.appendChild(actions);
    return row;
  }

  function renderStaff(list) {
    rows.replaceChildren();

    if (!list.length) {
      var emptyRow = document.createElement("tr");
      var emptyCell = cell("No staff found.");
      emptyCell.colSpan = 7;
      emptyRow.appendChild(emptyCell);
      rows.appendChild(emptyRow);
    } else {
      list.forEach(function (person) {
        rows.appendChild(buildRow(person));
      });
    }

    countLabel.textContent =
      list.length === 1
        ? "1 staff member shown"
        : list.length + " staff members shown";
  }

  async function loadStaff() {
    var sequence = ++loadSequence;
    var params = new URLSearchParams();
    var query = queryInput.value.trim();
    if (query) {
      params.set("q", query);
    }
    if (activeFilter.value) {
      params.set("active", activeFilter.value);
    }
    var suffix = params.toString() ? "?" + params.toString() : "";

    try {
      var response = await FA.request("/api/admin/staff" + suffix);
      if (!response.ok) {
        throw new Error(await FA.readError(response));
      }
      var list = await response.json();
      if (sequence !== loadSequence) {
        return;
      }
      renderStaff(list);
    } catch (error) {
      if (sequence !== loadSequence) {
        return;
      }
      setMessage(error.message || "Could not load staff.", "error");
      rows.replaceChildren();
      var errorRow = document.createElement("tr");
      var errorCell = cell("Could not load staff.");
      errorCell.colSpan = 7;
      errorRow.appendChild(errorCell);
      rows.appendChild(errorRow);
      countLabel.textContent = "";
    }
  }

  function openEditor(person) {
    form.reset();
    idInput.value = "";
    activeField.hidden = true;

    if (person) {
      idInput.value = String(person.id);
      codeInput.value = person.staff_code;
      nameInput.value = person.name;
      departmentInput.value = person.department || "";
      shiftInput.value = person.shift || "";
      activeInput.checked = person.active;
      activeField.hidden = false;
      editorTitle.textContent = "Edit staff: " + person.staff_code;
      saveButton.textContent = "Save changes";
    } else {
      codeInput.value = "";
      nameInput.value = "";
      departmentInput.value = "";
      shiftInput.value = "";
      activeInput.checked = true;
      editorTitle.textContent = "Add staff";
      saveButton.textContent = "Add staff";
    }

    setEditorError("");
    editor.hidden = false;
    editor.scrollIntoView({ behavior: "smooth", block: "start" });
    codeInput.focus();
  }

  function closeEditor() {
    editor.hidden = true;
    form.reset();
    idInput.value = "";
    setEditorError("");
  }

  async function saveStaff(event) {
    event.preventDefault();
    if (saving) {
      return;
    }

    var staffCode = codeInput.value.trim();
    var fullName = nameInput.value.trim();
    if (!staffCode || !fullName) {
      setEditorError("Staff code and full name are required.");
      (staffCode ? nameInput : codeInput).focus();
      return;
    }

    var payload = {
      staff_code: staffCode,
      name: fullName,
      department: departmentInput.value.trim() || null,
      shift: shiftInput.value.trim() || null,
    };
    var editingId = idInput.value ? Number(idInput.value) : null;
    if (editingId) {
      payload.active = activeInput.checked;
    }

    saving = true;
    saveButton.disabled = true;
    setEditorError("");

    try {
      var response = await FA.request(
        editingId ? "/api/admin/staff/" + editingId : "/api/admin/staff",
        {
          method: editingId ? "PATCH" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }
      );
      if (!response.ok) {
        throw new Error(await FA.readError(response));
      }
      var saved = await response.json();
      closeEditor();
      setMessage(
        editingId
          ? "Saved changes to " + saved.name + " (" + saved.staff_code + ")."
          : "Added " + saved.name + " (" + saved.staff_code + ").",
        "success"
      );
      await loadStaff();
    } catch (error) {
      setEditorError(error.message || "Could not save staff.");
    } finally {
      saving = false;
      saveButton.disabled = false;
    }
  }

  async function setActive(person, active, trigger) {
    trigger.disabled = true;
    try {
      var response = await FA.request("/api/admin/staff/" + person.id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: active }),
      });
      if (!response.ok) {
        throw new Error(await FA.readError(response));
      }
      setMessage(
        person.name + " is now " + (active ? "active." : "inactive."),
        "success"
      );
      await loadStaff();
    } catch (error) {
      trigger.disabled = false;
      setMessage(error.message || "Could not update active status.", "error");
    }
  }

  async function removeStaff(person, trigger) {
    var confirmed = window.confirm(
      "Delete " + person.name + " (" + person.staff_code + ")?\n\n" +
        "This removes their face embeddings, consent record and personal " +
        "details. Attendance history is kept. This cannot be undone."
    );
    if (!confirmed) {
      return;
    }

    trigger.disabled = true;
    try {
      var response = await FA.request("/api/admin/staff/" + person.id, {
        method: "DELETE",
      });
      if (!response.ok) {
        throw new Error(await FA.readError(response));
      }
      setMessage(
        "Deleted " + person.name + " (" + person.staff_code + ").",
        "success"
      );
      await loadStaff();
    } catch (error) {
      trigger.disabled = false;
      setMessage(error.message || "Could not delete staff.", "error");
    }
  }

  document.addEventListener("DOMContentLoaded", async function () {
    rows = byId("staff-rows");
    countLabel = document.querySelector("[data-count]");
    messageBox = byId("staff-message");
    editor = byId("staff-editor");
    editorTitle = byId("editor-title");
    editorError = byId("editor-error");
    form = byId("staff-form");
    idInput = byId("staff-id");
    codeInput = byId("staff-code");
    nameInput = byId("staff-name");
    departmentInput = byId("staff-department");
    shiftInput = byId("staff-shift");
    activeField = byId("staff-active-field");
    activeInput = byId("staff-active");
    saveButton = byId("save-staff");
    queryInput = byId("search-query");
    activeFilter = byId("active-filter");

    FA.wireLogout();
    try {
      var me = await FA.requireSession();
      FA.showAdminName(me.admin.username);
      FA.startSessionWatch();
    } catch (error) {
      return;
    }

    byId("add-staff").addEventListener("click", function () {
      openEditor(null);
    });
    byId("cancel-edit").addEventListener("click", closeEditor);
    form.addEventListener("submit", saveStaff);

    byId("search-form").addEventListener("submit", function (event) {
      event.preventDefault();
      loadStaff();
    });
    activeFilter.addEventListener("change", loadStaff);

    var debounce = null;
    queryInput.addEventListener("input", function () {
      window.clearTimeout(debounce);
      debounce = window.setTimeout(loadStaff, 300);
    });

    await loadStaff();
  });
})();
