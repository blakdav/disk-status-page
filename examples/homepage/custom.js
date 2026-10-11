// Disk Status Page: scoped text colors for Homepage v2.5.0.
// This does not fetch data or modify Homepage's independent health dot.
(() => {
  const slot = '__diskStatusTextColors';
  window[slot]?.disconnect();
  const roots = '#disk-status-details, #disk-status-summary';
  const classes = new Map([
    ['Awake', 'awake'], ['Sleeping', 'sleeping'], ['Healthy', 'healthy'],
    ['Testing', 'testing'], ['Needs attention', 'attention'],
    ['Known issue', 'known'], ['Health unknown', 'unknown'],
    ['Unknown', 'unknown'], ['Stale', 'stale'],
    ['Last read: testing', 'unknown'],
    ['Current', 'healthy'], ['Delayed', 'delayed'], ['Awaiting data', 'stale']
  ]);
  const icons = /[🟢🔵🟣🔴🟡⚪🟠]\s*/gu;
  const signature = new WeakMap();
  let scheduled = false;

  function paint() {
    scheduled = false;
    const summary = document.getElementById('disk-status-summary');
    function colorCount(label, value) {
      if (!label || !value) return;
      const name = label.textContent.replace(icons, '').trim().toLowerCase();
      const amount = Number(value.textContent.trim());
      if (!Number.isFinite(amount)) return;
      const tones = {awake: 'awake', sleeping: 'sleeping',
        attention: amount > 0 ? 'attention' : 'healthy',
        'known issues': amount > 0 ? 'known' : 'unknown'};
      if (tones[name]) value.dataset.dspStatus = tones[name];
    }
    // Summary metrics in block view or list view; zero attention is green.
    summary?.querySelectorAll('.service-block').forEach(block =>
      colorCount(block.lastElementChild, block.firstElementChild));
    summary?.querySelectorAll('div.font-thin.pl-2').forEach(label =>
      colorCount(label, label.parentElement.querySelector('div.font-bold.mr-2')));
    document.querySelectorAll(roots).forEach(root => {
      // v2.5.0 Custom API list/dynamic-list value nodes, not titles/errors.
      root.querySelectorAll('div.font-bold.mr-2').forEach(value => {
        const original = value.textContent;
        if (signature.get(value) === original && value.querySelector('[data-dsp-status]')) return;
        const clean = original.replace(icons, '').trim();
        const pieces = clean.split(' · ');
        if (!pieces.every(piece => classes.has(piece))) return;
        const fragment = document.createDocumentFragment();
        pieces.forEach((piece, index) => {
          if (index) fragment.append(document.createTextNode(' · '));
          const span = document.createElement('span');
          span.dataset.dspStatus = classes.get(piece);
          span.textContent = piece;
          fragment.append(span);
        });
        // No HTML interpolation: status content remains ordinary text.
        value.replaceChildren(fragment);
        signature.set(value, value.textContent);
      });
    });
  }
  function schedule() {
    if (!scheduled) {
      scheduled = true;
      requestAnimationFrame(paint);
    }
  }
  const observer = new MutationObserver(records => {
    if (records.some(record => {
      const element = record.target.nodeType === Node.ELEMENT_NODE
        ? record.target : record.target.parentElement;
      return element?.closest(roots) || [...record.addedNodes].some(node =>
        node.nodeType === Node.ELEMENT_NODE &&
        (node.matches(roots) || node.querySelector(roots)));
    })) schedule();
  });
  observer.observe(document.documentElement, {
    childList: true, subtree: true, characterData: true
  });
  window[slot] = observer;
  schedule();
})();
