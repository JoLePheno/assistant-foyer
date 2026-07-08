// Graphique de l'évolution de la température, avec code couleur :
//   < 21 °C → bleu, 21–26 °C → vert, > 26 °C → jaune.

const COLD = "#4a90d9";
const OK = "#5aa469";
const HOT = "#e6b73c";

function colorFor(t) {
  if (t == null) return OK;
  if (t < 21) return COLD;
  if (t < 26) return OK;
  return HOT;
}

function formatLabel(ts, hours) {
  const d = new Date(ts);
  if (hours <= 24) {
    return d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  }
  return (
    d.toLocaleDateString("fr-FR", { day: "numeric", month: "short" }) +
    " " +
    d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })
  );
}

let chart;

async function load(hours) {
  document.querySelectorAll(".ranges button").forEach((b) =>
    b.classList.toggle("active", Number(b.dataset.h) === hours)
  );

  let points = [];
  try {
    const data = await (await fetch(`/api/history?hours=${hours}`)).json();
    points = data.points || [];
  } catch {
    points = [];
  }

  const empty = document.getElementById("empty");
  const wrap = document.getElementById("chart-wrap");

  if (points.length < 2) {
    empty.style.display = "block";
    wrap.style.display = "none";
    if (chart) chart.destroy();
    return;
  }
  empty.style.display = "none";
  wrap.style.display = "block";

  const labels = points.map((p) => formatLabel(p.ts, hours));
  const temps = points.map((p) => p.temperature);
  const hums = points.map((p) => p.humidity);

  if (chart) chart.destroy();
  chart = new Chart(document.getElementById("chart"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Température",
          data: temps,
          tension: 0.3,
          borderWidth: 3,
          pointRadius: points.length > 60 ? 0 : 3,
          pointBackgroundColor: temps.map(colorFor),
          pointBorderColor: temps.map(colorFor),
          borderColor: OK,
          // couleur de chaque segment selon la température de départ
          segment: {
            borderColor: (ctx) => colorFor(ctx.p0.parsed.y),
          },
          fill: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => {
              const h = hums[c.dataIndex];
              return ` ${c.parsed.y} °C` + (h != null ? ` · ${Math.round(h)} % HR` : "");
            },
          },
        },
      },
      scales: {
        y: {
          title: { display: true, text: "°C" },
          grid: { color: "#efe9e1" },
          ticks: { color: "#9b948c" },
        },
        x: {
          grid: { display: false },
          ticks: { color: "#9b948c", maxTicksLimit: 8, autoSkip: true, maxRotation: 0 },
        },
      },
    },
  });
}

document.querySelectorAll(".ranges button").forEach((b) => {
  b.onclick = () => load(Number(b.dataset.h));
});

load(24);

