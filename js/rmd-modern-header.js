/**
 * R.M.D. ENGINEERING COLLEGE - MODERN HEADER & MOBILE DRAWER SCRIPT
 * 100% Vanilla JavaScript - Zero external libraries
 */

(function () {
  'use strict';

  function initHeader() {
    var hamburger = document.getElementById('rmd-hamburger-btn');
    var drawer = document.getElementById('rmd-mobile-drawer');
    var overlay = document.getElementById('rmd-drawer-overlay');
    var closeBtn = document.getElementById('rmd-drawer-close');

    if (!hamburger || !drawer || !overlay) return;

    function openDrawer() {
      drawer.classList.add('active');
      overlay.classList.add('active');
      document.body.style.overflow = 'hidden';
    }

    function closeDrawer() {
      drawer.classList.remove('active');
      overlay.classList.remove('active');
      document.body.style.overflow = '';
    }

    hamburger.addEventListener('click', openDrawer);
    if (closeBtn) closeBtn.addEventListener('click', closeDrawer);
    overlay.addEventListener('click', closeDrawer);

    // Close on Escape key
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && drawer.classList.contains('active')) {
        closeDrawer();
      }
    });

    // Mobile drawer accordion toggles
    var accordionBtns = drawer.querySelectorAll('.rmd-drawer-accordion-btn');
    accordionBtns.forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        var sublist = btn.nextElementSibling;
        if (!sublist) return;

        var isExpanded = sublist.classList.contains('active');
        // Close siblings
        var allSublists = drawer.querySelectorAll('.rmd-drawer-sublist');
        allSublists.forEach(function (list) {
          list.classList.remove('active');
        });

        if (!isExpanded) {
          sublist.classList.add('active');
          var arrow = btn.querySelector('.rmd-arrow');
          if (arrow) arrow.style.transform = 'rotate(180deg)';
        }
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initHeader);
  } else {
    initHeader();
  }
})();
