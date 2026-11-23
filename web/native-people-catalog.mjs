// Keep unqualified recognition actions separate from detection and saved edits.
const response = await fetch('/api/people-preview/capabilities');
if (response.ok) {
  const capabilities = await response.json();
  const update = () => {
    for (const button of document.querySelectorAll('button')) {
      const label = button.textContent.trim();
      const matching = ['Detect again', 'Auto-organize faces'].includes(label)
        && !capabilities.automatic_matching;
      if (!matching) continue;
      if (!button.disabled) button.disabled = true;
      button.title = 'Native automatic matching needs local calibration. Saved groups and manual review are available.';
    }
  };
  update();
  new MutationObserver(update).observe(document.body, {
    childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'],
  });
}
