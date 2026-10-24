(function () {
  // Mermaid is loaded from a local classic script, so this works from file://.
  if (window.mermaid) {
    var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    try {
      window.mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'loose',
        theme: dark ? 'dark' : 'neutral',
        flowchart: { useMaxWidth: true, htmlLabels: true },
        sequence: { useMaxWidth: true },
        themeVariables: {
          fontFamily: '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif'
        }
      });
      window.mermaid.run({ querySelector: '.mermaid' }).catch(function (err) {
        console.error('Mermaid render failed:', err);
      });
    } catch (err) {
      console.error('Mermaid init failed:', err);
    }
  }

  var body = document.body;
  var menuBtn = document.getElementById('menuBtn');
  var overlay = document.getElementById('overlay');

  function closeNav() {
    body.classList.remove('nav-open');
  }

  if (menuBtn) {
    menuBtn.addEventListener('click', function () {
      body.classList.toggle('nav-open');
    });
  }
  if (overlay) overlay.addEventListener('click', closeNav);
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') closeNav();
  });
  document.querySelectorAll('.sidebar a').forEach(function (link) {
    link.addEventListener('click', closeNav);
  });

  var filter = document.getElementById('navFilter');
  if (filter) {
    filter.addEventListener('input', function () {
      var query = filter.value.trim().toLowerCase();
      document.querySelectorAll('.nav-group').forEach(function (group) {
        var visible = 0;
        group.querySelectorAll('.nav-item').forEach(function (item) {
          var hit = !query || item.textContent.toLowerCase().indexOf(query) !== -1;
          item.hidden = !hit;
          if (hit) visible += 1;
        });
        group.hidden = visible === 0;
      });
    });
  }

  var toTop = document.getElementById('toTop');
  if (toTop) {
    function updateTop() {
      toTop.hidden = window.scrollY < 600;
    }
    window.addEventListener('scroll', updateTop, { passive: true });
    updateTop();
  }
})();
