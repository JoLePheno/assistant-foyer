const $ = (sel) => document.querySelector(sel);
const history = [];

// --- date du jour -----------------------------------------------------------
$("#date").textContent = new Date().toLocaleDateString("fr-FR", {
  weekday: "long", day: "numeric", month: "long",
});

// --- widgets (température + courses) ----------------------------------------
async function loadWidgets() {
  try {
    const data = await (await fetch("/api/widgets")).json();
    $("#temp").textContent = data.temperature.temperature;
    $("#hum").textContent = data.temperature.humidity + "%";
    $("#temp-source").textContent = data.temperature.source === "demo" ? "démo" : "";
    renderCourses(data.courses);
  } catch {
    $("#temp").textContent = "--";
  }
}

function renderCourses(courses) {
  const ul = $("#courses");
  ul.innerHTML = "";
  if (!courses.length) {
    ul.innerHTML = '<li class="empty">Liste vide pour le moment</li>';
    return;
  }
  for (const c of courses) {
    const li = document.createElement("li");
    if (c.done) li.classList.add("done");

    const check = document.createElement("span");
    check.className = "check";
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = c.item;
    const del = document.createElement("button");
    del.className = "del";
    del.setAttribute("aria-label", "Supprimer");
    del.textContent = "×";

    check.onclick = label.onclick = () => toggleCourse(c.id);
    del.onclick = () => deleteCourse(c.id);

    li.append(check, label, del);
    ul.appendChild(li);
  }
}

async function toggleCourse(id) {
  await fetch(`/api/courses/${id}/toggle`, { method: "POST" });
  loadWidgets();
}

async function deleteCourse(id) {
  await fetch(`/api/courses/${id}`, { method: "DELETE" });
  loadWidgets();
}

$("#course-form").onsubmit = async (e) => {
  e.preventDefault();
  const input = $("#course-input");
  const item = input.value.trim();
  if (!item) return;
  input.value = "";
  await fetch("/api/courses", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item }),
  });
  loadWidgets();
};

// --- chat -------------------------------------------------------------------
function addBubble(role, text) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  const chat = $("#chat");
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

$("#chat-form").onsubmit = async (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";

  addBubble("user", text);
  history.push({ role: "user", content: text });

  const pending = addBubble("assistant", "…");
  try {
    const data = await (await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history }),
    })).json();
    pending.textContent = data.reply;
    history.push({ role: "assistant", content: data.reply });
  } catch {
    pending.textContent = "⚠️ Erreur de connexion au serveur.";
  }
};

// --- démarrage --------------------------------------------------------------
loadWidgets();
