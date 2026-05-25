(() => {
  const STORAGE_KEY = 'ccs-theme';
  const root = document.documentElement;

  const normalizeTheme = (theme) => (theme === 'dark' ? 'dark' : 'light');

  function applyTheme(theme, persist = true) {
    const nextTheme = normalizeTheme(theme);

    // Toggle logic: data-theme drives CSS variables and icon styling globally.
    root.setAttribute('data-theme', nextTheme);
    if (persist) {
      localStorage.setItem(STORAGE_KEY, nextTheme);
    }

    document.querySelectorAll('[data-theme-toggle]').forEach((toggle) => {
      const isDark = nextTheme === 'dark';
      toggle.setAttribute('aria-pressed', String(isDark));
      toggle.dataset.themeState = nextTheme;
      const label = toggle.querySelector('.theme-toggle-text');
      if (label) {
        label.textContent = isDark ? 'Dark' : 'Light';
      }

      // Icon switching: use real files from static/images instead of text glyphs.
      const icon = toggle.querySelector('[data-theme-icon]');
      if (icon) {
        icon.src = isDark ? icon.dataset.darkSrc : icon.dataset.lightSrc;
      }
    });
  }

  function getCurrentTheme() {
    return normalizeTheme(root.getAttribute('data-theme') || localStorage.getItem(STORAGE_KEY));
  }

  function bindToggles() {
    document.querySelectorAll('[data-theme-toggle]').forEach((toggle) => {
      toggle.addEventListener('click', () => {
        const nextTheme = getCurrentTheme() === 'dark' ? 'light' : 'dark';
        applyTheme(nextTheme);
      });
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(getCurrentTheme(), false);
    bindToggles();
  });

  window.addEventListener('storage', (event) => {
    if (event.key === STORAGE_KEY) {
      applyTheme(event.newValue, false);
    }
  });

  window.CCSTheme = { applyTheme, getCurrentTheme };
})();
