/**
 * Educational & Student Redesign Disclaimer Banner
 * Automatically displays a non-intrusive legal notice at the top of the page.
 */
(function () {
  if (document.getElementById('edu-disclaimer-banner')) return;

  function renderDisclaimer() {
    if (document.getElementById('edu-disclaimer-banner')) return;

    var banner = document.createElement('div');
    banner.id = 'edu-disclaimer-banner';
    banner.style.cssText = 'background: linear-gradient(90deg, #0f172a 0%, #1e293b 100%); color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 13px; line-height: 1.5; padding: 10px 18px; border-bottom: 3px solid #f59e0b; position: relative; z-index: 999999; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.2);';

    banner.innerHTML = [
      '<div style="max-width: 1240px; margin: 0 auto; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">',
      '  <div style="display: flex; align-items: center; gap: 10px; text-align: left; flex: 1; min-width: 280px;">',
      '    <span style="background: #f59e0b; color: #0f172a; font-weight: 800; font-size: 11px; padding: 3px 9px; border-radius: 4px; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap;">Student Redesign Project</span>',
      '    <span style="color: #cbd5e1; font-size: 12.5px;"><strong>Notice &amp; Disclaimer:</strong> This website is an unofficial student educational redesign &amp; academic portfolio study. All institutional names, logos, trademarks, and original assets are the exclusive property of <strong>R.M.D. Engineering College</strong>. This project is entirely non-commercial and has no official institutional affiliation or endorsement.</span>',
      '  </div>',
      '  <div style="display: flex; align-items: center; gap: 12px; white-space: nowrap;">',
      '    <a href="https://www.rmd.ac.in" target="_blank" rel="noopener noreferrer" style="background: #2563eb; color: #ffffff !important; text-decoration: none; font-weight: 600; font-size: 12px; padding: 6px 14px; border-radius: 4px; display: inline-block;">Official College Site &rarr;</a>',
      '    <button id="close-edu-banner-btn" style="background: rgba(255,255,255,0.1); border: 1px solid #475569; color: #94a3b8; cursor: pointer; border-radius: 4px; padding: 4px 10px; font-size: 15px; line-height: 1;" title="Dismiss notice">&times;</button>',
      '  </div>',
      '</div>'
    ].join('\n');

    if (document.body.firstChild) {
      document.body.insertBefore(banner, document.body.firstChild);
    } else {
      document.body.appendChild(banner);
    }

    var closeBtn = document.getElementById('close-edu-banner-btn');
    if (closeBtn) {
      closeBtn.onclick = function () {
        banner.style.display = 'none';
      };
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderDisclaimer);
  } else {
    renderDisclaimer();
  }
})();
