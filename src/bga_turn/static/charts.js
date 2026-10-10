// Draws every <canvas data-chart="..."> from the JSON in <script id="chart-data">.
(() => {
  'use strict';
  const source = document.getElementById('chart-data');
  if (!source) return;
  if (typeof Chart === 'undefined') {
    // The chart library could not load (offline, blocked CDN): say so instead of
    // leaving an empty box.
    document.querySelectorAll('canvas[data-chart]').forEach((canvas) => {
      const note = document.createElement('div');
      note.className = 'chart-unavailable';
      note.textContent = 'Chart unavailable';
      canvas.replaceWith(note);
    });
    return;
  }

  let data;
  try {
    data = JSON.parse(source.textContent || '{}');
  } catch (error) {
    return;
  }

  const styles = getComputedStyle(document.documentElement);
  const color = (name, fallback) => (styles.getPropertyValue(name) || fallback).trim();
  const accent = color('--accent', '#5865f2');
  Chart.defaults.color = color('--text-muted', '#9a9db3');
  Chart.defaults.borderColor = color('--border', '#2b2e42');
  Chart.defaults.font.family = color('--font', 'system-ui');

  const integerAxis = {beginAtZero: true, ticks: {precision: 0}};

  const builders = {
    activity: (canvas, compact) => ({
      type: 'line',
      data: {
        labels: (data.games_over_time || []).map((row) => row.day),
        datasets: [{
          label: 'Games finished',
          data: (data.games_over_time || []).map((row) => row.count),
          borderColor: accent,
          backgroundColor: 'rgba(88,101,242,0.15)',
          fill: true,
          tension: 0.3,
          pointRadius: compact ? 0 : 2,
          pointHoverRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {mode: 'index', intersect: false},
        plugins: {legend: {display: false}},
        scales: {
          x: {ticks: {maxTicksLimit: compact ? 6 : 10}, grid: {display: false}},
          y: compact ? {...integerAxis, display: false} : integerAxis,
        },
      },
    }),
    games: () => ({
      type: 'bar',
      data: {
        labels: (data.games_by_name || []).map((row) => row.name),
        datasets: [{
          label: 'Games',
          data: (data.games_by_name || []).map((row) => row.count),
          backgroundColor: 'rgba(88,101,242,0.75)',
          borderRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: 'y',
        plugins: {legend: {display: false}},
        scales: {x: integerAxis, y: {grid: {display: false}}},
      },
    }),
  };

  document.querySelectorAll('canvas[data-chart]').forEach((canvas) => {
    const build = builders[canvas.dataset.chart];
    if (build) new Chart(canvas, build(canvas, canvas.dataset.compact === '1'));
  });
})();
