(function () {
  var wrap = document.getElementById('lm-wrap');
  if (!wrap) return;

  function syncScrollHint() {
    var canX = wrap.scrollWidth > wrap.clientWidth + 2;
    var canY = wrap.scrollHeight > wrap.clientHeight + 2;
    wrap.classList.toggle('has-x-scroll', canX);
    wrap.classList.toggle('has-y-scroll', canY);
  }

  wrap.addEventListener('scroll', function () {
    wrap.classList.add('is-scrolling');
  });

  document.addEventListener('lm-rows-added', syncScrollHint);
  syncScrollHint();
  window.addEventListener('resize', syncScrollHint);
  window.addEventListener('load', syncScrollHint);
})();
