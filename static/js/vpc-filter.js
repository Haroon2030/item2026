(function () {
  'use strict';

  var branchEl = document.getElementById('vpc-branch');
  var whEl = document.getElementById('vpc-wh');
  var dataEl = document.getElementById('vpc-wh-data');
  var vendorEl = document.getElementById('vpc-vendor');
  var vendorFilter = document.getElementById('vpc-q');
  var vendorSuggest = document.getElementById('vpc-vendor-suggestions');
  var vendorActive = -1;

  var allWh = [];
  if (dataEl) {
    try {
      allWh = JSON.parse(dataEl.textContent || '[]');
    } catch (e) {
      allWh = [];
    }
  }

  function fillWarehouses(branchCode, selectedCode) {
    if (!whEl) return;
    var brn = String(branchCode || '').trim();
    var keep = String(selectedCode || '').trim();
    whEl.innerHTML = '';

    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'كل المخازن';
    whEl.appendChild(placeholder);

    var list = brn
      ? allWh.filter(function (w) {
          return String(w.branch_code || '') === brn;
        })
      : allWh.slice();

    list
      .slice()
      .sort(function (a, b) {
        return String(a.code).localeCompare(String(b.code), 'ar', {
          numeric: true,
        });
      })
      .forEach(function (w) {
        var opt = document.createElement('option');
        opt.value = w.code;
        opt.title = w.name || w.code;
        opt.textContent = w.code + ' — ' + (w.name || w.code);
        if (keep && String(keep) === String(w.code)) {
          opt.selected = true;
        }
        whEl.appendChild(opt);
      });
  }

  if (branchEl && whEl) {
    branchEl.addEventListener('change', function () {
      fillWarehouses(branchEl.value, '');
    });
  }

  function vendorItems() {
    if (!vendorEl) return [];
    var out = [];
    for (var i = 0; i < vendorEl.options.length; i++) {
      var opt = vendorEl.options[i];
      if (!opt.value) continue;
      out.push({
        code: String(opt.value),
        label: String(opt.textContent || '').trim(),
      });
    }
    return out;
  }

  function closeVendorSuggest() {
    if (!vendorSuggest || !vendorFilter) return;
    vendorSuggest.hidden = true;
    vendorSuggest.innerHTML = '';
    vendorFilter.setAttribute('aria-expanded', 'false');
    vendorActive = -1;
  }

  function pickVendor(code, label) {
    if (!vendorEl || !vendorFilter) return;
    vendorEl.value = code || '';
    vendorFilter.value = label || '';
    closeVendorSuggest();
  }

  function renderVendorSuggest(q) {
    if (!vendorSuggest || !vendorFilter) return;
    var query = String(q || '').trim().toLowerCase();
    var items = vendorItems();
    var matches = [];
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var hay = (it.label + ' ' + it.code).toLowerCase();
      if (!query || hay.indexOf(query) !== -1) matches.push(it);
      if (matches.length >= 15) break;
    }
    vendorSuggest.innerHTML = '';
    if (!query) {
      closeVendorSuggest();
      return;
    }
    if (!matches.length) {
      var empty = document.createElement('li');
      empty.className = 'vendor-suggest-empty';
      empty.textContent = 'لا توجد نتائج في موردي الشركة';
      vendorSuggest.appendChild(empty);
    } else {
      matches.forEach(function (it, idx) {
        var li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.id = 'vpc-vendor-opt-' + idx;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'vendor-suggest-option';
        btn.setAttribute('data-code', it.code);
        btn.setAttribute('data-label', it.label);
        var name = document.createElement('span');
        name.className = 'vendor-suggest-name';
        name.textContent = it.label.split(' · ')[0] || it.label;
        var code = document.createElement('span');
        code.className = 'vendor-suggest-code';
        code.textContent = it.code;
        btn.appendChild(name);
        btn.appendChild(code);
        btn.addEventListener('mousedown', function (e) {
          e.preventDefault();
          pickVendor(it.code, it.label);
        });
        li.appendChild(btn);
        vendorSuggest.appendChild(li);
      });
    }
    vendorSuggest.hidden = false;
    vendorFilter.setAttribute('aria-expanded', 'true');
    vendorActive = -1;
  }

  function moveVendorActive(delta) {
    if (!vendorSuggest || vendorSuggest.hidden) return;
    var opts = vendorSuggest.querySelectorAll('.vendor-suggest-option');
    if (!opts.length) return;
    vendorActive = (vendorActive + delta + opts.length) % opts.length;
    opts.forEach(function (el, i) {
      if (i === vendorActive) el.classList.add('is-active');
      else el.classList.remove('is-active');
    });
    opts[vendorActive].scrollIntoView({ block: 'nearest' });
  }

  if (vendorFilter) {
    vendorFilter.addEventListener('input', function () {
      if (!String(vendorFilter.value || '').trim()) {
        if (vendorEl) vendorEl.value = '';
        closeVendorSuggest();
        return;
      }
      if (vendorEl) vendorEl.value = '';
      renderVendorSuggest(vendorFilter.value);
    });
    vendorFilter.addEventListener('focus', function () {
      if (String(vendorFilter.value || '').trim()) {
        renderVendorSuggest(vendorFilter.value);
      }
    });
    vendorFilter.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (vendorSuggest && vendorSuggest.hidden) {
          renderVendorSuggest(vendorFilter.value);
        }
        moveVendorActive(1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        moveVendorActive(-1);
      } else if (e.key === 'Enter') {
        var opts = vendorSuggest
          ? vendorSuggest.querySelectorAll('.vendor-suggest-option')
          : [];
        if (
          vendorSuggest &&
          !vendorSuggest.hidden &&
          vendorActive >= 0 &&
          opts[vendorActive]
        ) {
          e.preventDefault();
          var btn = opts[vendorActive];
          pickVendor(
            btn.getAttribute('data-code'),
            btn.getAttribute('data-label')
          );
        } else if (vendorSuggest && !vendorSuggest.hidden && opts.length === 1) {
          e.preventDefault();
          pickVendor(
            opts[0].getAttribute('data-code'),
            opts[0].getAttribute('data-label')
          );
        }
      } else if (e.key === 'Escape') {
        closeVendorSuggest();
      }
    });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.vpc-vendor-suggest')) closeVendorSuggest();
    });
  }
})();
