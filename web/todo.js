const $ = (sel) => document.querySelector(sel);
const OWNERS = ["marion", "jonathan"];

async function loadTodos() {
  let data;
  try {
    data = await (await fetch("/api/todos")).json();
  } catch {
    return;
  }
  for (const owner of OWNERS) {
    renderList(owner, data.todos[owner] || []);
  }
}

function renderList(owner, items) {
  const ul = document.querySelector(`#list-${owner}`);
  ul.innerHTML = "";
  if (!items.length) {
    ul.innerHTML = '<li class="empty">Rien pour le moment 🎉</li>';
    return;
  }
  for (const t of items) {
    const li = document.createElement("li");
    if (t.done) li.classList.add("done");

    const check = document.createElement("span");
    check.className = "check";
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = t.item;
    const del = document.createElement("button");
    del.className = "del";
    del.setAttribute("aria-label", "Supprimer");
    del.textContent = "×";

    check.onclick = label.onclick = () => toggleTodo(t.id);
    del.onclick = () => deleteTodo(t.id);

    li.append(check, label, del);
    ul.appendChild(li);
  }
}

async function toggleTodo(id) {
  await fetch(`/api/todos/${id}/toggle`, { method: "POST" });
  loadTodos();
}

async function deleteTodo(id) {
  await fetch(`/api/todos/${id}`, { method: "DELETE" });
  loadTodos();
}

document.querySelectorAll("form[data-owner]").forEach((form) => {
  form.onsubmit = async (e) => {
    e.preventDefault();
    const input = form.querySelector("input");
    const item = input.value.trim();
    if (!item) return;
    input.value = "";
    await fetch("/api/todos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner: form.dataset.owner, item }),
    });
    loadTodos();
  };
});

loadTodos();

