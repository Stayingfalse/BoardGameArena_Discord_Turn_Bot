// Shared dashboard behaviour. Loaded on every page; no inline scripts so the
// Content-Security-Policy can forbid them.
(() => {
  'use strict';

  // Confirmation prompts: <form data-confirm="Are you sure?">
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const message = form.dataset.confirm;
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    // Avoid double submissions.
    form.querySelectorAll('button[type=submit]').forEach((button) => {
      button.disabled = true;
    });
  });

  // Re-enable buttons when the page is restored from the back/forward cache.
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('button[type=submit]:disabled').forEach((button) => {
      if (!button.dataset.keepDisabled) button.disabled = false;
    });
  });

  // Drop one-shot status flags (?saved=1, ?error=...) from the address bar so a
  // reload does not show the message again.
  const url = new URL(window.location.href);
  let changed = false;
  for (const key of ['saved', 'removed', 'linked', 'unlinked', 'error', 'auth_result', 'login_attempt']) {
    if (url.searchParams.has(key)) {
      url.searchParams.delete(key);
      changed = true;
    }
  }
  if (changed) window.history.replaceState(null, '', url.pathname + url.search + url.hash);

  // Member picker: <div class="picker" data-search-url="..."> with an input,
  // a results list and a hidden discord_user_id field.
  document.querySelectorAll('.picker').forEach((picker) => {
    const input = picker.querySelector('.picker-input');
    const results = picker.querySelector('.picker-results');
    const hidden = picker.querySelector('input[name=discord_user_id]');
    const chosen = picker.querySelector('.picker-chosen');
    const chosenLabel = picker.querySelector('.picker-chosen-label');
    const clear = picker.querySelector('.picker-clear');
    const form = picker.closest('form');
    const submit = form ? form.querySelector('button[type=submit]') : null;
    if (!input || !results || !hidden) return;

    let timer = null;
    let requestId = 0;
    let options = [];
    let active = -1;

    const setSubmitState = () => {
      if (!submit) return;
      submit.disabled = !hidden.value;
      submit.dataset.keepDisabled = hidden.value ? '' : '1';
    };
    setSubmitState();

    const close = () => {
      results.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      active = -1;
    };

    const status = (text) => {
      results.innerHTML = '';
      const li = document.createElement('li');
      li.className = 'picker-status';
      li.textContent = text;
      results.appendChild(li);
      results.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    };

    const choose = (member) => {
      hidden.value = member.id;
      if (chosen && chosenLabel) {
        chosenLabel.textContent = `${member.display_name} (@${member.username})`;
        chosen.hidden = false;
        input.hidden = true;
      }
      close();
      setSubmitState();
      if (submit) submit.focus();
    };

    const highlight = (index) => {
      const items = results.querySelectorAll('.picker-option');
      items.forEach((item, i) => item.setAttribute('aria-selected', i === index ? 'true' : 'false'));
      active = index;
      if (items[index]) items[index].scrollIntoView({block: 'nearest'});
    };

    const render = (members) => {
      options = members;
      results.innerHTML = '';
      if (!members.length) {
        status('No matching members. Search by username or nickname prefix, or paste a user ID.');
        return;
      }
      members.forEach((member, index) => {
        const li = document.createElement('li');
        li.className = 'picker-option';
        li.setAttribute('role', 'option');
        li.id = `${input.id}-opt-${index}`;
        if (member.avatar_url) {
          const img = document.createElement('img');
          img.className = 'avatar';
          img.src = member.avatar_url;
          img.alt = '';
          li.appendChild(img);
        }
        const text = document.createElement('span');
        const name = document.createElement('strong');
        name.textContent = member.display_name;
        const sub = document.createElement('span');
        sub.className = 'sub';
        sub.textContent = ` @${member.username}`;
        text.append(name, sub);
        li.appendChild(text);
        li.addEventListener('mousedown', (event) => {
          event.preventDefault();
          choose(member);
        });
        results.appendChild(li);
      });
      results.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      highlight(0);
    };

    const search = async (query) => {
      const current = ++requestId;
      status('Searching…');
      try {
        const response = await fetch(`${picker.dataset.searchUrl}?q=${encodeURIComponent(query)}`, {
          headers: {Accept: 'application/json'},
          credentials: 'same-origin',
        });
        const payload = await response.json().catch(() => ({}));
        if (current !== requestId) return;
        if (!response.ok) {
          status(payload.error || 'Search failed. Try again.');
          return;
        }
        render(payload.members || []);
      } catch (error) {
        if (current === requestId) status('Search failed. Check your connection.');
      }
    };

    input.addEventListener('input', () => {
      hidden.value = '';
      setSubmitState();
      const value = input.value.trim();
      if (timer) clearTimeout(timer);
      if (!value) {
        close();
        return;
      }
      timer = setTimeout(() => search(value), 250);
    });

    input.addEventListener('keydown', (event) => {
      if (results.hidden || !options.length) return;
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        highlight(Math.min(active + 1, options.length - 1));
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        highlight(Math.max(active - 1, 0));
      } else if (event.key === 'Enter') {
        event.preventDefault();
        if (options[active]) choose(options[active]);
      } else if (event.key === 'Escape') {
        close();
      }
    });

    input.addEventListener('blur', () => setTimeout(close, 120));

    if (clear) {
      clear.addEventListener('click', () => {
        hidden.value = '';
        if (chosen) chosen.hidden = true;
        input.hidden = false;
        input.value = '';
        input.focus();
        setSubmitState();
      });
    }
  });
})();
