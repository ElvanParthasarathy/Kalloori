/**
 * R.M.D. ENGINEERING COLLEGE - MODERN FLAT HERO CAROUSEL CONTROLLER
 * 100% Vanilla JavaScript - Touch-Enabled & Accessible
 */

(function () {
  'use strict';

  function initCarousel() {
    var carousel = document.getElementById('rmd-hero-carousel');
    if (!carousel) return;

    var slides = carousel.querySelectorAll('.rmd-slide');
    var indicators = carousel.querySelectorAll('.rmd-indicator');
    var prevBtn = carousel.querySelector('.rmd-carousel-prev');
    var nextBtn = carousel.querySelector('.rmd-carousel-next');

    if (!slides.length) return;

    var currentIndex = 0;
    var timer = null;
    var INTERVAL = 5500;

    function goToSlide(index) {
      slides[currentIndex].classList.remove('active');
      if (indicators[currentIndex]) {
        indicators[currentIndex].classList.remove('active');
      }

      currentIndex = (index + slides.length) % slides.length;

      slides[currentIndex].classList.add('active');
      if (indicators[currentIndex]) {
        indicators[currentIndex].classList.add('active');
      }
    }

    function nextSlide() {
      goToSlide(currentIndex + 1);
    }

    function prevSlide() {
      goToSlide(currentIndex - 1);
    }

    function startAutoPlay() {
      stopAutoPlay();
      timer = setInterval(nextSlide, INTERVAL);
    }

    function stopAutoPlay() {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    }

    // Prev / Next Listeners
    if (nextBtn) {
      nextBtn.addEventListener('click', function (e) {
        e.preventDefault();
        nextSlide();
        startAutoPlay();
      });
    }

    if (prevBtn) {
      prevBtn.addEventListener('click', function (e) {
        e.preventDefault();
        prevSlide();
        startAutoPlay();
      });
    }

    // Indicator clicks
    indicators.forEach(function (ind, idx) {
      ind.addEventListener('click', function () {
        goToSlide(idx);
        startAutoPlay();
      });
    });

    // Pause on hover
    carousel.addEventListener('mouseenter', stopAutoPlay);
    carousel.addEventListener('mouseleave', startAutoPlay);

    // Touch Swipe Support
    var touchStartX = 0;
    var touchEndX = 0;

    carousel.addEventListener('touchstart', function (e) {
      touchStartX = e.changedTouches[0].screenX;
      stopAutoPlay();
    }, { passive: true });

    carousel.addEventListener('touchend', function (e) {
      touchEndX = e.changedTouches[0].screenX;
      var diff = touchStartX - touchEndX;
      if (Math.abs(diff) > 40) {
        if (diff > 0) nextSlide();
        else prevSlide();
      }
      startAutoPlay();
    }, { passive: true });

    // Keyboard navigation
    document.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowRight') nextSlide();
      if (e.key === 'ArrowLeft') prevSlide();
    });

    // Start
    startAutoPlay();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCarousel);
  } else {
    initCarousel();
  }
})();
