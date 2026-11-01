/* Navigation and clipboard are progressive enhancements; every route works without JS. */
document.documentElement.classList.add('js');
const menu = document.querySelector('[data-menu]');
const nav = document.querySelector('#primary-nav');
if (menu && nav) {
  menu.hidden = false;
  const setOpen = (open) => {
    menu.setAttribute('aria-expanded', String(open));
    nav.dataset.open = String(open);
    menu.textContent = open ? 'Close' : 'Menu';
  };
  menu.addEventListener('click', () => setOpen(menu.getAttribute('aria-expanded') !== 'true'));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && menu.getAttribute('aria-expanded') === 'true') {
      setOpen(false);
      menu.focus();
    }
  });
}
if (navigator.clipboard && window.isSecureContext) {
  for (const button of document.querySelectorAll('[data-copy]')) {
    button.hidden = false;
    button.addEventListener('click', async () => {
      const code = button.parentElement.querySelector('code');
      const status = button.parentElement.querySelector('[role="status"]');
      try {
        await navigator.clipboard.writeText(code.textContent);
        status.textContent = 'Copied';
        button.setAttribute('aria-label', 'Copied to clipboard');
      } catch {
        status.textContent = 'Select the code to copy it.';
      }
      window.setTimeout(() => {
        status.textContent = '';
        button.setAttribute('aria-label', 'Copy code');
      }, 2500);
    });
  }
}
