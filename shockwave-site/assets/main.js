(function () {
  // Mobile sidebar toggle
  var menuBtn = document.querySelector('.menu-btn');
  var sidebar = document.querySelector('.sidebar');
  var scrim = document.querySelector('.scrim');

  function closeSidebar() {
    sidebar.classList.remove('open');
    scrim.classList.remove('show');
    menuBtn.setAttribute('aria-expanded', 'false');
  }

  function openSidebar() {
    sidebar.classList.add('open');
    scrim.classList.add('show');
    menuBtn.setAttribute('aria-expanded', 'true');
  }

  if (menuBtn && sidebar && scrim) {
    menuBtn.addEventListener('click', function () {
      var isOpen = sidebar.classList.contains('open');
      isOpen ? closeSidebar() : openSidebar();
    });
    scrim.addEventListener('click', closeSidebar);
    document.querySelectorAll('.sidebar .nav-link').forEach(function (link) {
      link.addEventListener('click', closeSidebar);
    });
  }

  // Highlight active nav link based on current page + hash
  var here = window.location.pathname.split('/').pop() || 'index.html';
  document.querySelectorAll('.nav-link').forEach(function (link) {
    var href = link.getAttribute('href') || '';
    var hrefPage = href.split('#')[0] || 'index.html';
    if (hrefPage === here || (hrefPage === '' && here === 'index.html')) {
      link.classList.add('active');
    }
  });

  // Stagger the embed-preview "deal in" animation instead of firing all at once
  var embeds = document.querySelectorAll('.discord-embed');
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  if (!reduceMotion && 'IntersectionObserver' in window) {
    var groups = {};
    embeds.forEach(function (el) {
      var group = el.closest('.embed-stack');
      if (!group) return;
      if (!groups[group.dataset.group || Math.random()]) {
        // fall back: assign a stable key per stack element
      }
    });

    var stacks = document.querySelectorAll('.embed-stack');
    stacks.forEach(function (stack) {
      var items = stack.querySelectorAll('.discord-embed');
      items.forEach(function (el, i) {
        el.style.animationDelay = (i * 0.12) + 's';
        el.style.animationPlayState = 'paused';
      });

      var obs = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            items.forEach(function (el) {
              el.style.animationPlayState = 'running';
            });
            obs.unobserve(stack);
          }
        });
      }, { threshold: 0.25 });

      obs.observe(stack);
    });
  } else {
    embeds.forEach(function (el) {
      el.style.opacity = '1';
      el.style.transform = 'none';
      el.style.animation = 'none';
    });
  }
})();
