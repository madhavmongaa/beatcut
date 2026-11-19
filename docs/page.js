/* beatcut live surface: page-local behaviour.
   Every panel computes from assets/sample.json, which beatcut itself produced
   (tools/make_sample.py). The engine (scrollcraft.js) is untouched. */
(function () {
  'use strict';

  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  var fine = matchMedia('(hover: hover) and (pointer: fine)').matches;
  var clamp = function (x, a, b) { return x < a ? a : x > b ? b : x; };
  var lerp = function (a, b, t) { return a + (b - a) * t; };
  var ease = function (x) { x = clamp(x, 0, 1); return x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; };
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var binds = {};
  $$('[data-bind]').forEach(function (el) { (binds[el.dataset.bind] = binds[el.dataset.bind] || []).push(el); });
  function set(key, text) {
    (binds[key] || []).forEach(function (el) { if (el.textContent !== text) el.textContent = text; });
  }
  function clock(t) {
    var m = Math.floor(t / 60), s = t - m * 60;
    return String(m).padStart(2, '0') + ':' + s.toFixed(2).padStart(5, '0');
  }
  function mmss(t) { var m = Math.floor(t / 60); return m + ':' + (t - m * 60).toFixed(1).padStart(4, '0'); }

  var INK = '#ebe7df', SOFT = '#9a958d', DIM = '#55524d', LINE = '#232325', ACCENT = '#ff5a1f';
  var LEVEL_FILL = { '-1': '#2a2826', '0': '#6b5e54', '1': ACCENT };

  function fit(canvas) {
    var r = canvas.getBoundingClientRect(), d = Math.min(devicePixelRatio || 1, 2);
    var w = Math.max(1, Math.round(r.width * d)), h = Math.max(1, Math.round(r.height * d));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    var ctx = canvas.getContext('2d');
    ctx.setTransform(d, 0, 0, d, 0, 0);
    return { ctx: ctx, w: r.width, h: r.height };
  }

  function actProgress(el) {
    var r = el.getBoundingClientRect(), vh = innerHeight;
    return clamp(-r.top / Math.max(1, r.height - vh), 0, 1);
  }
  function onScreen(el) { var r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight; }

  fetch('assets/sample.json').then(function (r) { return r.json(); }).then(start).catch(function (err) {
    set('status', 'Could not load the demo project. Serve this folder over http to run it.');
    console.error(err);
  });

  function start(D) {
    var FPS = D.fps, DUR = D.duration, TRACK = D.track_duration;
    var levelAt = function (i) { return D.beat_levels[i] || 0; };
    var downSet = new Set(D.downbeats.map(function (t) { return t.toFixed(4); }));
    var isDown = D.beats.map(function (t) { return downSet.has(t.toFixed(4)); });

    // A port of plan.cut_times, so the Options panel can re-cut from the real beats.
    function cutTimes(every, duration, startAt) {
      var steps = { '1': Math.max(1, Math.floor(every / 2)), '0': every, '-1': every * 2 };
      var cuts = [], last = 0;
      for (var i = 0; i < D.beats.length; i++) {
        var rel = D.beats[i] - startAt;
        if (rel < 0) continue;
        if (rel > duration - 0.25) break;
        var s = steps[String(levelAt(i))];
        if ((((i - D.phase) % s) + s) % s) continue;
        var snapped = Math.round(rel * FPS) / FPS;
        if (snapped - last < 0.25) continue;
        cuts.push(snapped); last = snapped;
      }
      return cuts;
    }
    var check = cutTimes(D.every, DUR, 0);
    console.assert(check.length === D.cuts.length && check.every(function (c, i) { return Math.abs(c - D.cuts[i]) < 1e-3; }),
      'cutTimes port disagrees with beatcut', check, D.cuts);

    // Bars: [start, end, level] across the whole track.
    var bars = [];
    D.beats.forEach(function (t, i) {
      if (!isDown[i]) return;
      var next = D.beats.slice(i + 1).find(function (u, k) { return isDown[i + 1 + k]; });
      bars.push([t, next || Math.min(TRACK, t + 60 / D.tempo * D.meter), levelAt(i)]);
    });
    var loudBars = bars.filter(function (b) { return b[2] === 1; }).length;

    set('trackLen', mmss(TRACK));
    set('cutTotal', String(D.cuts.length));

    function drawPeaks(ctx, w, h, t0, t1, colourFn, mid) {
      var rate = D.peak_rate, peaks = D.peaks, cols = Math.max(1, Math.floor(w / 2));
      for (var c = 0; c < cols; c++) {
        var a = t0 + (t1 - t0) * c / cols, b = t0 + (t1 - t0) * (c + 1) / cols;
        var i0 = Math.floor(a * rate), i1 = Math.max(i0 + 1, Math.floor(b * rate)), v = 0;
        for (var i = i0; i < i1 && i < peaks.length; i++) if (peaks[i] > v) v = peaks[i];
        var amp = (v / 255) * (mid);
        ctx.fillStyle = colourFn(a);
        ctx.fillRect(c * 2, h / 2 - amp, 1.4, Math.max(1, amp * 2));
      }
    }

    /* ---------- 1 · hero ---------- */
    var hero = $('#project'), heroWave = $('.hero__wave'), scene = $('.scene');
    var bin = $('.bin'), mon = $('.monitor--hero'), fly = $('.flying'), slot = $('.bin__slot');
    var pointer = { x: 0, y: 0, tx: 0, ty: 0 };
    var heroGeo = null;
    function heroLayout() {
      [bin, mon, fly].forEach(function (el) { el.style.transform = 'none'; });
      var sc = scene.getBoundingClientRect(), sl = slot.getBoundingClientRect(), mr = mon.getBoundingClientRect();
      var w = sl.width + 12;
      fly.style.width = w + 'px';
      fly.style.left = (sl.left - sc.left - 6) + 'px';
      fly.style.top = (sl.top - sc.top - 6) + 'px';
      var fh = fly.getBoundingClientRect().height;
      var slotCx = sl.left - sc.left + sl.width / 2, slotCy = sl.top - sc.top + (fh - 12) / 2;
      var airCx = mr.left - sc.left - mr.width * 0.06, airCy = mr.top - sc.top + mr.height * 0.16;
      heroGeo = { dx: airCx - slotCx, dy: airCy - slotCy };
      var f = fit(heroWave), ctx = f.ctx;
      ctx.clearRect(0, 0, f.w, f.h);
      drawPeaks(ctx, f.w, f.h, 0, TRACK, function () { return 'rgba(235,231,223,.075)'; }, f.h * 0.48);
      ctx.fillStyle = 'rgba(255,90,31,.22)';
      D.downbeats.forEach(function (t) { ctx.fillRect(Math.round(t / TRACK * f.w), f.h * 0.5 - 3, 1, 6); });
    }
    if (fine && !reduce) {
      addEventListener('pointermove', function (e) {
        pointer.tx = e.clientX / innerWidth - 0.5; pointer.ty = e.clientY / innerHeight - 0.5;
      }, { passive: true });
    }
    function heroFrame() {
      if (!heroGeo || !onScreen(hero)) return;
      pointer.x = lerp(pointer.x, pointer.tx, 0.08); pointer.y = lerp(pointer.y, pointer.ty, 0.08);
      var s = reduce ? 0 : Math.min(scrollY, innerHeight);
      var px = pointer.x, py = pointer.y;
      heroWave.style.transform = 'translate3d(' + (px * -8) + 'px,' + (s * 0.42) + 'px,0)';
      var bx = px * -12, by = s * 0.18 + py * -8;
      bin.style.transform = 'translate3d(' + bx + 'px,' + by + 'px,0)';
      mon.style.transform = 'translate3d(' + (px * -20) + 'px,' + (s * 0.07 + py * -14) + 'px,0)';
      var e = reduce ? 0.12 : ease(Math.min(scrollY, innerHeight) / (innerHeight * 0.62));
      var ax = heroGeo.dx + px * -30, ay = heroGeo.dy + py * -22;
      var x = lerp(ax, bx, e), y = lerp(ay, by, e);
      fly.style.transform = 'translate3d(' + x + 'px,' + y + 'px,0) rotate(' + lerp(-8, 0, e) + 'deg) scale(' + lerp(1.42, 1, e) + ')';
      var lift = 1 - e;
      fly.style.boxShadow = '0 ' + (36 * lift + 2) + 'px ' + (60 * lift + 4) + 'px -' + (14 * lift) + 'px rgba(0,0,0,' + (0.75 * lift + 0.2) + ')';
      var landed = e > 0.995;
      slot.classList.toggle('is-filled', landed);
      fly.classList.toggle('is-landed', landed);
      hero.setAttribute('data-sc-verify-state', e.toFixed(2));
    }

    /* ---------- 2 · track ---------- */
    var trackAct = $('#track'), trackState = $('[data-verify="track"]');
    var tw = $('.js-trackwave'), en = $('.js-energy'), lastSweep = -1;
    function trackFrame(force) {
      if (!onScreen(trackAct) && !force) return;
      var p = actProgress(trackAct);
      var sweep = reduce ? (p > 0.02 ? 1 : 0.0) : clamp((p - 0.04) / 0.8, 0, 1);
      if (Math.abs(sweep - lastSweep) < 0.0005 && !force) return;
      lastSweep = sweep;
      var at = sweep * TRACK;
      var f = fit(tw), ctx = f.ctx, w = f.w, h = f.h;
      ctx.clearRect(0, 0, w, h);
      drawPeaks(ctx, w, h, 0, TRACK, function (t) { return t <= at ? '#8a847c' : '#3a3836'; }, h * 0.36);
      ctx.font = '10px ui-monospace, SF Mono, Menlo, monospace';
      ctx.textBaseline = 'top';
      var bar = 0;
      D.beats.forEach(function (t, i) {
        if (isDown[i]) bar++;
        if (t > at) return;
        var x = Math.round(t / TRACK * w) + 0.5;
        ctx.fillStyle = isDown[i] ? INK : 'rgba(235,231,223,.38)';
        ctx.fillRect(x - 0.5, isDown[i] ? 14 : h * 0.14, 1, isDown[i] ? h - 14 : h * 0.72);
        if (isDown[i] && (w > 700 || bar % 2 === 1)) { ctx.fillStyle = SOFT; ctx.fillText(String(bar), x + 3, 0); }
      });
      if (sweep > 0 && sweep < 1) {
        var sx = Math.round(at / TRACK * w);
        ctx.fillStyle = ACCENT; ctx.fillRect(sx - 1, 0, 2, h);
      }
      var g = fit(en), c2 = g.ctx;
      c2.clearRect(0, 0, g.w, g.h);
      c2.fillStyle = '#121213'; c2.fillRect(0, 0, g.w, g.h);
      var loud = 0;
      bars.forEach(function (b) {
        if (b[0] > at) return;
        var x0 = b[0] / TRACK * g.w, x1 = Math.min(b[1], at) / TRACK * g.w;
        c2.fillStyle = LEVEL_FILL[String(b[2])];
        c2.fillRect(x0 + 1, 0, Math.max(0, x1 - x0 - 2), g.h);
        if (b[2] === 1) loud++;
      });
      var found = D.beats.filter(function (t) { return t <= at; }).length;
      set('beatsFound', String(found));
      var tempoOn = sweep > 0.3, meterOn = sweep > 0.42;
      set('tempo', tempoOn ? D.tempo.toFixed(1) + ' BPM' : 'listening');
      set('meter', meterOn ? D.meter + '/4' : 'listening');
      set('loud', loud + ' of ' + bars.length);
      (binds.tempo || []).forEach(function (el) { el.classList.toggle('is-wait', !tempoOn); });
      (binds.meter || []).forEach(function (el) { el.classList.toggle('is-wait', !meterOn); });
      if (tempoOn) bpmKnown = true;
      trackState.setAttribute('data-sc-verify-state', sweep.toFixed(3));
    }
    var bpmKnown = false;

    /* ---------- 3 · clips ---------- */
    var list = $('.js-cliplist');
    var maxM = 0;
    D.clips.forEach(function (c) { c.motion.forEach(function (v) { if (v > maxM) maxM = v; }); });
    D.clips.slice().sort(function (a, b) {
      return D.segments.filter(function (s) { return s.clip === b.name; }).length -
             D.segments.filter(function (s) { return s.clip === a.name; }).length;
    }).forEach(function (c) {
      var segs = D.segments.filter(function (s) { return s.clip === c.name; });
      var n = c.motion.length, W = 600, H = 36;
      var pts = c.motion.map(function (v, i) {
        return (i / (n - 1) * W).toFixed(1) + ',' + (H - Math.sqrt(v / maxM) * (H - 3) - 1).toFixed(1);
      }).join(' ');
      var picks = segs.map(function (s) {
        var x = s.clip_start / c.duration * W, w = (s.end - s.start) / c.duration * W;
        return '<rect x="' + x.toFixed(1) + '" y="0" width="' + Math.max(1.5, w).toFixed(1) + '" height="' + H + '" fill="rgba(255,90,31,.32)"/>';
      }).join('');
      var li = document.createElement('li');
      var stem = c.name.replace(/\.mp4$/, '');
      li.innerHTML = '<span class="name"><img src="assets/thumb-' + stem + '.jpg" width="64" height="36" alt=""><span class="mono">' + c.name + '</span></span>' +
        '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" role="img" aria-label="Motion in ' + c.name + ', ' + segs.length + ' stretches picked">' +
        '<line x1="0" x2="' + W + '" y1="' + (H - 0.5) + '" y2="' + (H - 0.5) + '" stroke="' + LINE + '" vector-effect="non-scaling-stroke"/>' + picks +
        '<polyline points="' + pts + '" fill="none" stroke="' + INK + '" stroke-width="1.2" vector-effect="non-scaling-stroke"/></svg>' +
        '<span class="n mono">' + segs.length + '</span>';
      list.appendChild(li);
    });

    /* ---------- 4 · cut (the peak) ---------- */
    var cutAct = $('#cut'), cutState = $('[data-verify="cut"]'), monitor = $('.js-monitor');
    var video = $('.js-video'), tl = $('.js-tl'), strip = $('.js-strip');
    var segEls = D.segments.map(function (s) {
      var i = document.createElement('i');
      i.style.left = (s.start / DUR * 100) + '%';
      i.style.width = ((s.end - s.start) / DUR * 100) + '%';
      i.style.backgroundImage = 'url(assets/thumb-' + s.clip.replace(/\.mp4$/, '') + '.jpg)';
      strip.appendChild(i);
      return i;
    });
    var vt = 0, lastCutIdx = -1, lastT = -1, flashTimer = 0, playing = false;
    var audio = new Audio('assets/sample-118.m4a');
    audio.preload = 'none';
    var playBtn = $('.js-play');
    function stopAudio() {
      if (!playing) return;
      playing = false; audio.pause(); playBtn.setAttribute('aria-pressed', 'false'); playBtn.textContent = 'Play with sound';
    }
    playBtn.addEventListener('click', function () {
      if (playing) { stopAudio(); return; }
      var from = clamp(lastT, 0, DUR - 0.5);
      if (audio.readyState >= 1) audio.currentTime = from;
      else audio.addEventListener('loadedmetadata', function () { audio.currentTime = from; }, { once: true });
      audio.play().then(function () {
        playing = true; playBtn.setAttribute('aria-pressed', 'true'); playBtn.textContent = 'Stop';
      }).catch(function () { set('say', 'Your browser blocked the sound. Scroll to play the edit instead.'); });
    });
    audio.addEventListener('ended', stopAudio);
    addEventListener('wheel', stopAudio, { passive: true });
    addEventListener('touchmove', stopAudio, { passive: true });

    fetch('assets/edit.mp4').then(function (r) { return r.blob(); }).then(function (b) {
      video.src = URL.createObjectURL(b);
      video.addEventListener('loadeddata', function () { monitor.classList.add('has-video'); }, { once: true });
      video.load();
    }).catch(function () { /* the poster frame stays */ });

    function beatIndexAt(t) {
      var k = 0;
      while (k + 1 < D.beats.length && D.beats[k + 1] <= t) k++;
      return k;
    }
    var SAY = {
      '-1': 'Quiet bars: a cut every four beats.',
      '0': 'Normal bars: a cut every two beats.',
      '1': 'Loud bars: a cut on every beat.'
    };
    function cutFrame() {
      if (!onScreen(cutAct)) { stopAudio(); return; }
      var t = playing ? audio.currentTime : clamp((actProgress(cutAct) - 0.03) / 0.92, 0, 1) * DUR;
      t = clamp(t, 0, DUR - 0.001);
      var idx = 0;
      while (idx < D.cuts.length && D.cuts[idx] <= t) idx++;
      if (lastCutIdx >= 0 && idx !== lastCutIdx && !reduce) {
        monitor.classList.add('is-cut'); led.classList.add('is-hit');
        clearTimeout(flashTimer);
        flashTimer = setTimeout(function () { monitor.classList.remove('is-cut'); led.classList.remove('is-hit'); }, 110);
      }
      lastCutIdx = idx;
      if (Math.abs(t - lastT) > 0.0005) {
        lastT = t;
        var f = fit(tl), ctx = f.ctx, w = f.w, h = f.h, top = 16;
        ctx.clearRect(0, 0, w, h);
        ctx.font = '10px ui-monospace, SF Mono, Menlo, monospace'; ctx.textBaseline = 'top';
        for (var s = 0; s <= DUR; s += 5) {
          var gx = Math.round(s / DUR * (w - 1)) + 0.5;
          ctx.fillStyle = SOFT; ctx.fillText(s + 's', Math.min(gx + 3, w - 22), 0);
          ctx.fillStyle = LINE; ctx.fillRect(gx - 0.5, top, 1, h - top);
        }
        ctx.save(); ctx.translate(0, top);
        drawPeaks(ctx, w, h - top, 0, DUR, function (u) { return u <= t ? '#7d7770' : '#38363a'; }, (h - top) * 0.34);
        D.beats.forEach(function (b, i) {
          if (b > DUR) return;
          var x = Math.round(b / DUR * w) + 0.5;
          ctx.fillStyle = isDown[i] ? 'rgba(235,231,223,.5)' : 'rgba(235,231,223,.18)';
          ctx.fillRect(x - 0.5, (h - top) - 8, 1, 8);
        });
        D.cuts.forEach(function (c, i) {
          var x = Math.round(c / DUR * w);
          if (c <= t) {
            ctx.fillStyle = ACCENT;
            ctx.fillRect(x - 1, 0, 2, h - top - 10);
            ctx.fillRect(x - 3, 0, 6, 4);
          } else {
            ctx.fillStyle = 'rgba(255,90,31,.22)';
            ctx.fillRect(x, 6, 1, h - top - 22);
          }
        });
        var px = Math.round(t / DUR * w);
        ctx.fillStyle = INK; ctx.fillRect(px - 1, -top, 2, h);
        ctx.restore();

        var segIdx = idx; // segment idx starts at cut idx
        segEls.forEach(function (el, i) {
          el.classList.toggle('on', D.segments[i].start <= t);
          el.classList.toggle('now', i === segIdx);
        });
        set('shotName', D.segments[segIdx] ? D.segments[segIdx].clip : '');
        set('clock', clock(t));
        set('statusTime', clock(t));
        set('cutNo', String(idx));
        set('say', SAY[String(levelAt(beatIndexAt(t)))]);
        cutState.setAttribute('data-sc-verify-state', t.toFixed(2));
      }
      // Follow the playhead with the editor's preview.
      if (video.readyState >= 2) {
        vt = reduce || playing ? t : lerp(vt, t, 0.35);
        if (Math.abs(video.currentTime - vt) > (playing ? 0.12 : 0.03) && !video.seeking) video.currentTime = vt;
      }
    }

    /* ---------- 5 · options ---------- */
    var opts = { every: '2', duration: '30', start: '0', aspect: '9:16', pick: 'motion', crop: 'centre', encoder: 'x264', match_energy: false };
    var SIZES = { '9:16': '1080 × 1920', '1:1': '1080 × 1080', '16:9': '1920 × 1080' };
    var cmd = $('.js-cmd');
    function optStart() { return opts.start === 'auto' ? D.best_start[opts.duration] : 0; }
    function renderOptions() {
      $$('.seg button').forEach(function (b) {
        b.setAttribute('aria-checked', String(opts[b.dataset.opt] === b.dataset.val));
        b.tabIndex = opts[b.dataset.opt] === b.dataset.val ? 0 : -1;
      });
      var dur = +opts.duration, st = optStart(), cuts = cutTimes(+opts.every, dur, st);
      set('optCuts', cuts.length + ' cuts in ' + dur + ' s');
      // cut grid for this setting
      var f = fit($('.js-mini-every')), ctx = f.ctx;
      ctx.clearRect(0, 0, f.w, f.h);
      bars.forEach(function (b) {
        var x0 = (b[0] - st) / dur * f.w, x1 = (b[1] - st) / dur * f.w;
        if (x1 < 0 || x0 > f.w) return;
        ctx.fillStyle = LEVEL_FILL[String(b[2])];
        ctx.globalAlpha = b[2] === 1 ? 0.35 : 0.6;
        ctx.fillRect(x0 + 1, f.h - 6, x1 - x0 - 2, 6);
      });
      ctx.globalAlpha = 1;
      D.beats.forEach(function (b) {
        var x = (b - st) / dur * f.w; if (x < 0 || x > f.w) return;
        ctx.fillStyle = 'rgba(235,231,223,.22)'; ctx.fillRect(Math.round(x), f.h - 16, 1, 8);
      });
      ctx.fillStyle = ACCENT;
      cuts.forEach(function (c) { ctx.fillRect(Math.round(c / dur * f.w) - 0.5, 6, 1.5, f.h - 24); });
      // window on the whole song
      var g = fit($('.js-mini-start')), c2 = g.ctx;
      c2.clearRect(0, 0, g.w, g.h);
      drawPeaks(c2, g.w, g.h, 0, TRACK, function (t) {
        var lv = 0; bars.forEach(function (b) { if (t >= b[0] && t < b[1]) lv = b[2]; });
        return t >= st && t <= st + dur ? (lv === 1 ? ACCENT : '#a59d93') : '#3a3836';
      }, g.h * 0.36);
      var wx0 = st / TRACK * g.w, wx1 = Math.min(TRACK, st + dur) / TRACK * g.w;
      c2.strokeStyle = INK; c2.lineWidth = 1.5; c2.strokeRect(wx0 + 0.75, 1, wx1 - wx0 - 1.5, g.h - 2);
      set('optStart', mmss(st) + ' to ' + mmss(Math.min(TRACK, st + dur)));
      var fr = $('.js-aspect');
      fr.style.aspectRatio = opts.aspect.replace(':', ' / ');
      fr.style.height = opts.aspect === '16:9' ? '120px' : opts.aspect === '1:1' ? '170px' : '190px';
      set('optSize', SIZES[opts.aspect]);
      $('[data-opt="match_energy"]').checked = opts.match_energy;
      // the command, carried to the close
      var c = 'beatcut --clips ./clips --track song.mp3 --out final.mp4';
      if (opts.duration !== '30') c += ' --duration ' + opts.duration;
      if (opts.every !== '2') c += ' --every ' + opts.every;
      if (opts.aspect !== '9:16') c += ' --aspect ' + opts.aspect;
      if (opts.start === 'auto') c += ' --start auto';
      if (opts.pick !== 'motion') c += ' --pick ' + opts.pick;
      if (opts.crop !== 'centre') c += ' --crop ' + opts.crop;
      if (opts.match_energy) c += ' --match-energy';
      if (opts.encoder !== 'x264') c += ' --encoder ' + opts.encoder;
      if (document.activeElement !== cmd) cmd.value = c;
    }
    $$('.seg button').forEach(function (b) {
      b.addEventListener('click', function () { opts[b.dataset.opt] = b.dataset.val; renderOptions(); });
      b.addEventListener('keydown', function (e) {
        if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].indexOf(e.key) < 0) return;
        e.preventDefault();
        var sibs = $$('button', b.parentNode), k = sibs.indexOf(b);
        var nb = sibs[(k + (e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 1) + sibs.length) % sibs.length];
        opts[nb.dataset.opt] = nb.dataset.val; renderOptions(); nb.focus();
      });
    });
    $('[data-opt="match_energy"]').addEventListener('change', function (e) { opts.match_energy = e.target.checked; renderOptions(); });

    // Keyboard focus inside the pinned rail: park the act where that control is on screen.
    var optAct = $('#options'), rail = $('.rail');
    rail.addEventListener('focusin', function (e) {
      var item = e.target.closest('.opt'); if (!item) return;
      var travel = rail.scrollWidth - innerWidth;
      if (travel <= 0 || reduce) return;
      var want = clamp((item.offsetLeft + item.offsetWidth / 2 - innerWidth / 2) / travel, 0, 1);
      var r = optAct.getBoundingClientRect();
      scrollTo({ top: scrollY + r.top + want * (r.height - innerHeight), behavior: 'instant' });
    });

    /* ---------- 6 · install ---------- */
    function copy(text, btn) {
      var done = function () {
        btn.classList.add('is-done'); var was = btn.textContent; btn.textContent = 'Copied';
        setTimeout(function () { btn.classList.remove('is-done'); btn.textContent = was; }, 1400);
      };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, function () {});
      else { cmd.focus(); cmd.select(); try { document.execCommand('copy'); done(); } catch (e) {} }
    }
    $$('.copy').forEach(function (b) { b.addEventListener('click', function () { copy(b.dataset.copy, b); }); });
    $('.js-copycmd').addEventListener('click', function (e) { copy(cmd.value, e.currentTarget); });

    /* ---------- chrome: tabs and status ---------- */
    var led = $('.led');
    var sections = ['project', 'track', 'clips', 'cut', 'options', 'install'].map(function (id) { return document.getElementById(id); });
    var STATUS = {
      project: 'Project open · 8 clips, 1 track',
      track: 'Analysing sample-118.wav',
      clips: 'Scored motion in 8 clips',
      cut: 'Cutting · ' + D.cuts.length + ' cuts, ' + D.segments.length + ' shots, nothing reused',
      options: 'Options · re-planned from the same analysis',
      install: 'Ready · copy the command'
    };
    var current = '';
    function chromeFrame() {
      var mid = innerHeight * 0.45, id = 'project';
      sections.forEach(function (s) { if (s.getBoundingClientRect().top <= mid) id = s.id; });
      if (id !== current) {
        current = id;
        $$('.tabs a').forEach(function (a) { a.setAttribute('aria-current', String(a.dataset.tab === id)); });
        set('status', STATUS[id]);
      }
      if (id === 'track') set('status', lastSweep >= 1 ? 'Analysed sample-118.wav · ' + D.beats.length + ' beats, ' + bars.length + ' bars' : 'Analysing sample-118.wav');
      set('statusBpm', bpmKnown || sections.indexOf(document.getElementById(id)) > 1 ? D.tempo.toFixed(1) + ' BPM' : '… BPM');
    }

    function layout() { heroLayout(); lastSweep = -1; lastT = -1; trackFrame(true); renderOptions(); }
    addEventListener('resize', layout);
    document.fonts.ready.then(layout);
    layout();
    (function loop() {
      heroFrame(); trackFrame(false); cutFrame(); chromeFrame();
      requestAnimationFrame(loop);
    })();
  }
})();
