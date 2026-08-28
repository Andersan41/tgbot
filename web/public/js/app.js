// =====================================================
// Trading Signal Bot — Dashboard Frontend
// =====================================================

const WS_URL = `ws://${location.host}/ws`;
let socket = null;
let priceChart = null;
let reconnectTimer = null;
let _lastWhaleSignals = new Set();
let _whaleSoundEnabled = true;
let _whaleAudioCtx = null;
let _currentTF = '1h';

function _timeAgo(ts) {
  const diff = Date.now() - ts;
  const min = Math.floor(diff / 60000);
  if (min < 1) return 'сейчас';
  if (min < 60) return min + 'м назад';
  const hr = Math.floor(min / 60);
  if (hr < 24) return hr + 'ч назад';
  return Math.floor(hr / 24) + 'д назад';
}

// ── Init ────────────────────────────────────────────
function connect() {
  socket = new WebSocket(WS_URL);

  socket.onopen = () => {
    updateStatus('Анализ активен');
    clearTimeout(reconnectTimer);
    hideLoader();
  };

  socket.onclose = () => {
    updateStatus('Переподключение...');
    showLoader('Переподключение к серверу...');
    reconnectTimer = setTimeout(connect, 3000);
  };

  socket.onerror = () => updateStatus('Ошибка соединения');

  socket.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'update') renderDashboard(data);
      if (data.type === 'open_trades') renderOpenTrades(data.trades || []);
      if (data.type === 'whale_test' && data.whale) renderWhale(data.whale);
    } catch (err) {
      console.error('Parse error:', err);
    }
  };
}

// ── Render ──────────────────────────────────────────
function renderDashboard(data) {
  const { indicators, structure, liquidity, levels, signal, priceHistory, price, symbol, error, openInterest, volumeProfile, bookAnomalies, whale } = data;

  if (error) {
    updateStatus(`Ошибка: ${error}`);
    return;
  }

  const sym = (symbol || '').replace('/USDT', '');
  updateStatus(`Анализ активен · <span class="sym">${sym}</span> $${Number(price || 0).toLocaleString()}`);

  if (indicators) renderIndicators(indicators);
  if (signal) renderSignal(signal, price);
  if (levels) renderLevelsData(levels);
  if (structure) renderSMC(structure, liquidity);
  if (priceHistory) updatePriceChart(priceHistory);
  if (openInterest) renderOpenInterest(openInterest);
  if (volumeProfile) renderVolumeProfile(volumeProfile, price);
  if (bookAnomalies) renderBookAnomalies(bookAnomalies);
  if (whale) renderWhale(whale);

  renderVerdictFromSignal(signal, indicators);
}

// ── Indicators ──────────────────────────────────────
function renderIndicators(ind) {
  // RSI
  const rsiSignal = ind.rsi > 70 ? 'bearish' : ind.rsi > 55 ? 'bullish' : ind.rsi > 45 ? 'neutral' : ind.rsi > 30 ? 'bearish' : 'bullish';
  renderCard('rsi', ind.rsi?.toFixed(1) || '—', rsiLabel(ind.rsi), rsiSignal, ind.rsi);

  // MACD
  const macdSig = ind.macd_bullish_cross ? 'bullish_cross' : ind.macd_bearish_cross ? 'bearish_cross' : ind.macd_hist > 0 ? 'bullish' : 'bearish';
  const macdLabel = ind.macd_bullish_cross ? 'Бычье пересечение' : ind.macd_bearish_cross ? 'Медвежье пересечение' : ind.macd_hist > 0 ? 'Выше нуля' : 'Ниже нуля';
  renderCard('macd', ind.macd_hist?.toFixed(2) || '—', macdLabel, macdSig, 50 + (ind.macd_hist || 0) * 10);

  // EMA
  const emaSig = ind.ema_bullish_alignment ? 'bullish' : ind.ema_bearish_alignment ? 'bearish' : 'neutral';
  const emaLabel = ind.ema_bullish_cross ? 'Бычье пересечение' : ind.ema_bearish_cross ? 'Медвежье пересечение' :
    ind.ema_bullish_alignment ? 'Aligned ↑' : ind.ema_bearish_alignment ? 'Aligned ↓' : 'Neutr.';
  renderCard('ema', ind.ema_fast?.toFixed(0) || '—', emaLabel, emaSig, 50);

  // ADX
  const adxSig = ind.trend_is_strong ? (ind.dmi_plus > ind.dmi_minus ? 'bullish' : 'bearish') : 'neutral';
  const adxLabel = ind.trend_is_strong ? `Trend (${ind.dmi_plus?.toFixed(1)} / ${ind.dmi_minus?.toFixed(1)})` : `Flat (${ind.adx?.toFixed(1)})`;
  renderCard('adx', ind.adx?.toFixed(1) || '—', adxLabel, adxSig, Math.min(100, (ind.adx || 0) * 2.5));

  // Supertrend
  const stSig = ind.supertrend_bullish ? 'bullish' : 'bearish';
  const stLabel = ind.supertrend_bullish ? 'Bullish trend' : 'Bearish trend';
  renderCard('st', ind.supertrend?.toFixed(0) || '—', stLabel, stSig, ind.supertrend_bullish ? 70 : 30);

  // Volume
  const volSig = ind.volume_above_avg ? 'bullish' : 'bearish';
  const volLabel = ind.volume_above_avg ? `Выше SMA (${((ind.volume / ind.volume_sma) * 100).toFixed(0)}%)` : 'Ниже среднего';
  const volPct = ind.volume_sma > 0 ? Math.min(100, (ind.volume / ind.volume_sma) * 50) : 50;
  renderCard('vol', ind.volume?.toFixed(0) || '—', volLabel, volSig, volPct);
}

function rsiLabel(rsi) {
  if (!rsi) return '—';
  if (rsi >= 70) return 'Перекупленность';
  if (rsi >= 55) return 'Зона роста';
  if (rsi >= 45) return 'Нейтрально';
  if (rsi >= 30) return 'Зона снижения';
  return 'Перепроданность';
}

function renderCard(id, value, sub, signal, barWidth) {
  setText(`val-${id}`, value);
  setText(`sub-${id}`, sub);

  const signalMap = {
    bullish:       { text: 'Рост',       cls: 'badge-bull', bar: 'bar-green'  },
    bullish_cross: { text: 'Рост',       cls: 'badge-bull', bar: 'bar-green'  },
    bearish:       { text: 'Падение',    cls: 'badge-bear', bar: 'bar-red'    },
    bearish_cross: { text: 'Падение',    cls: 'badge-bear', bar: 'bar-red'    },
    neutral:       { text: 'Нейтрально', cls: 'badge-neu',  bar: 'bar-orange' }
  };
  const s = signalMap[signal] || signalMap.neutral;

  const badge = document.getElementById(`badge-${id}`);
  if (badge) { badge.textContent = s.text; badge.className = `badge ${s.cls}`; }

  const bar = document.getElementById(`bar-${id}`);
  if (bar) {
    bar.className = `bar-fill ${s.bar}`;
    bar.style.width = clamp(barWidth ?? 50, 0, 100) + '%';
  }
}

// ── Signal ──────────────────────────────────────────
function renderSignal(sig, currentPrice) {
  const card = document.getElementById('signalCard');
  const typeEl = document.getElementById('signalType');
  const verdictEl = document.getElementById('signalVerdict');
  const confFill = document.getElementById('signalConfFill');
  const confLabel = document.getElementById('signalConfLabel');
  const tsEl = document.getElementById('signalTimestamp');
  const plBox = document.getElementById('signalPlBox');

  const signal = sig.signal || 'NO_SIGNAL';
  const isBuy = signal === 'BUY';
  const isSell = signal === 'SELL';
  const score = sig.score || 0;
  const conf = sig.confidence || 0;

  card.className = `signal-card ${isBuy ? 'buy' : isSell ? 'sell' : 'no'}`;
  typeEl.className = `signal-type ${isBuy ? 'buy' : isSell ? 'sell' : 'no'}`;
  typeEl.textContent = signal === 'BUY' ? 'BUY — ПОКУПКА' : signal === 'SELL' ? 'SELL — ПРОДАЖА' : 'НЕТ СИГНАЛА';

  verdictEl.textContent = `${score > 0 ? '+' : ''}${score}  ${sig.verdict || ''}`;
  verdictEl.className = `signal-verdict ${isBuy ? 'buy' : isSell ? 'sell' : 'no'}`;

  confFill.style.width = conf + '%';
  confFill.className = `signal-conf-fill ${isBuy ? 'buy' : isSell ? 'sell' : 'no'}`;
  confLabel.textContent = conf.toFixed(0) + '%';

  if (sig.timestamp) {
    const d = new Date(sig.timestamp);
    tsEl.textContent = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  } else {
    tsEl.textContent = '';
  }

  setText('signalEntry', sig.entry ? `$${sig.entry.toLocaleString()}` : '—');
  setText('signalSL', sig.sl ? `$${sig.sl.toLocaleString()}` : '—');
  setText('signalTP', sig.tp ? `$${sig.tp.toLocaleString()}` : '—');

  // P/L от входа
  if (sig.entry && currentPrice) {
    const diff = currentPrice - sig.entry;
    const pct = (diff / sig.entry * 100);
    const plEl = document.getElementById('signalPL');
    if (plEl) {
      const sign = diff >= 0 ? '+' : '';
      plEl.textContent = `${sign}$${diff.toFixed(2)} (${sign}${pct.toFixed(2)}%)`;
      plEl.className = `signal-detail-value ${diff >= 0 ? 'pl-pos' : 'pl-neg'}`;
    }
    plBox.style.display = '';
  } else {
    plBox.style.display = 'none';
  }

  // Reasons — color-coded with ✓/✗ and values
  const reasonsEl = document.getElementById('signalReasons');
  if (sig.reasons && sig.reasons.length > 0) {
    reasonsEl.innerHTML = sig.reasons.map(r => {
      const text = r.text || r.reason || r;
      const val = r.value || '';
      const passed = r.passed !== undefined ? r.passed : (typeof r === 'string');
      const icon = passed ? '✓' : '✗';
      const cls = passed ? 'reason-pass' : 'reason-fail';
      return `<span class="reason-item ${cls}"><span class="reason-icon">${icon}</span>${escapeHtml(text)}${val ? '<span class="reason-val">' + escapeHtml(val) + '</span>' : ''}</span>`;
    }).join('');
  } else {
    reasonsEl.innerHTML = '';
  }
}

// ── Levels ──────────────────────────────────────────
function renderLevelsData(levels) {
  renderLevels(levels.resistance || [], 'resistance-list', false);
  renderLevels(levels.support || [], 'support-list', true);
}

function renderLevels(items, containerId, isSupport) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const strCls = { strong: 'str-strong', medium: 'str-medium', weak: 'str-weak' };
  const strRu = { strong: 'Сильный', medium: 'Средний', weak: 'Слабый' };

  container.innerHTML = items.map(lvl => `
    <div class="level-row">
      <span class="${isSupport ? 'level-dot-g' : 'level-dot-r'}"></span>
      <span class="level-price">$${Number(lvl.price || 0).toLocaleString(undefined, {minimumFractionDigits: 4, maximumFractionDigits: 4})}</span>
      <span class="str-pill ${strCls[lvl.strength] || 'str-medium'}">${strRu[lvl.strength] || 'Средний'}</span>
    </div>
  `).join('') || '<div style="font-size:11px;color:#444;padding:4px 0;">Нет данных</div>';
}

// ── Verdict ─────────────────────────────────────────
function renderVerdictFromSignal(signal, indicators) {
  const verdictMain = document.getElementById('verdict-main');
  const verdictConf = document.getElementById('verdict-conf');
  const verdictRegime = document.getElementById('verdict-regime');
  const verdictTrend = document.getElementById('verdict-trend');
  const confBar = document.getElementById('conf-bar');

  if (!signal || !verdictMain) return;

  const conf = signal.confidence || 0;
  const verdict = signal.verdict || '—';
  const isBull = signal.signal === 'BUY';
  const isBear = signal.signal === 'SELL';

  verdictMain.textContent = verdict;
  verdictMain.style.color = isBull ? '#a3e635' : isBear ? '#f87171' : '#facc15';
  verdictConf.textContent = `Confidence: ${conf.toFixed(1)}%`;
  confBar.style.width = clamp(conf, 0, 100) + '%';

  setText('verdict-regime', signal.regime || '—');
  setText('verdict-trend', indicators?.trend_is_strong ? 'Strong' : 'Weak');
}

// ── SMC ─────────────────────────────────────────────
function renderSMC(structure, liquidity) {
  const container = document.getElementById('smc-list');
  if (!container) return;

  const items = [];

  // BOS
  if (structure?.bos && structure.bos.type !== 'none') {
    items.push({
      iconCls: structure.bos.type === 'bullish' ? 'smc-icon-green' : 'smc-icon-red',
      icon: structure.bos.type === 'bullish' ? '↗' : '↘',
      name: 'Break of Structure',
      desc: `${structure.bos.type} @ $${(structure.bos.level || 0).toLocaleString()}`,
      badgeCls: structure.bos.type === 'bullish' ? 'smc-bull' : 'smc-target',
      badge: structure.bos.type === 'bullish' ? '+Bull' : '-Bear'
    });
  }

  // Order Blocks
  if (liquidity?.order_blocks) {
    liquidity.order_blocks.slice(0, 2).forEach(ob => {
      items.push({
        iconCls: 'smc-icon-blue', icon: '▣',
        name: 'Order Block',
        desc: `${ob.type} @ $${(ob.price || 0).toLocaleString()}`,
        badgeCls: 'smc-support', badge: 'Поддержка'
      });
    });
  }

  // FVG
  if (liquidity?.fvg) {
    liquidity.fvg.slice(0, 1).forEach(f => {
      items.push({
        iconCls: 'smc-icon-yellow', icon: '═',
        name: 'Fair Value Gap',
        desc: `$${(f.bottom || 0).toLocaleString()} — $${(f.top || 0).toLocaleString()}`,
        badgeCls: 'smc-magnet', badge: 'Магнит'
      });
    });
  }

  // Sweep
  if (liquidity?.sweep?.detected) {
    items.push({
      iconCls: 'smc-icon-red', icon: '⚠',
      name: 'Liquidity Sweep',
      desc: `${liquidity.sweep.type} sweep detected`,
      badgeCls: 'smc-target', badge: 'Цель'
    });
  }

  // Trend
  if (structure?.trend) {
    items.push({
      iconCls: structure.trend === 'bullish' ? 'smc-icon-green' : structure.trend === 'bearish' ? 'smc-icon-red' : 'smc-icon-yellow',
      icon: '◈',
      name: 'Market Trend',
      desc: structure.trend,
      badgeCls: structure.trend === 'bullish' ? 'smc-bull' : structure.trend === 'bearish' ? 'smc-target' : 'smc-magnet',
      badge: structure.trend
    });
  }

  container.innerHTML = items.map(it => `
    <div class="smc-item">
      <div class="smc-icon ${it.iconCls}">${it.icon}</div>
      <div>
        <div class="smc-name">${it.name}</div>
        <div class="smc-desc">${it.desc}</div>
      </div>
      <span class="smc-badge ${it.badgeCls}">${it.badge}</span>
    </div>
  `).join('') || '<div style="font-size:11px;color:#444;padding:4px 0;">Нет данных SMC</div>';
}

// ── Chart ───────────────────────────────────────────
function updatePriceChart(history) {
  const canvas = document.getElementById('priceChart');
  if (!canvas || !history || history.length === 0) return;

  const labels = history.map((_, i) => `H${i + 1}`);
  const prices = history.map(h => h.close);

  if (!priceChart) {
    const ctx = canvas.getContext('2d');
    priceChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data: prices,
          borderColor: '#a3e635',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.4,
          fill: true,
          backgroundColor: (ctx) => {
            const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, 110);
            g.addColorStop(0, 'rgba(163,230,53,0.15)');
            g.addColorStop(1, 'rgba(163,230,53,0)');
            return g;
          }
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 200 },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: { label: c => '$' + c.raw.toLocaleString() },
            backgroundColor: '#1a1a1a', titleColor: '#888',
            bodyColor: '#e0e0e0', borderColor: '#2a2a2a', borderWidth: 1
          }
        },
        scales: {
          x: { ticks: { color: '#555', font: { size: 9 } }, grid: { color: '#1a1a1a' } },
          y: {
            ticks: { color: '#555', font: { size: 9 }, callback: v => '$' + (v/1000).toFixed(1) + 'k' },
            grid: { color: '#1e1e1e' }
          }
        }
      }
    });
  } else {
    priceChart.data.labels = labels;
    priceChart.data.datasets[0].data = prices;
    priceChart.update('none');
  }
}

// ── Open Interest ──────────────────────────────────
function renderOpenInterest(oi) {
  const value = oi.current || 0;
  const change = oi.change_pct || 0;
  const trend = oi.trend || 'none';

  setText('val-oi', value > 1000000 ? (value / 1000000).toFixed(2) + 'M' : value > 1000 ? (value / 1000).toFixed(1) + 'K' : value.toFixed(0));

  const changeEl = document.getElementById('oi-change');
  if (changeEl) {
    const sign = change >= 0 ? '+' : '';
    changeEl.textContent = `${sign}${change.toFixed(1)}%`;
    changeEl.className = `oi-change ${change > 0 ? 'bull' : change < 0 ? 'bear' : 'neu'}`;
  }

  const trendEl = document.getElementById('oi-trend');
  if (trendEl) {
    const trendMap = { increasing: 'Растёт', decreasing: 'Снижается', stable: 'Стабилен' };
    trendEl.textContent = trendMap[trend] || '—';
    trendEl.className = `oi-detail-value ${trend === 'increasing' ? 'bull' : trend === 'decreasing' ? 'bear' : 'neu'}`;
  }

  setText('oi-value-usd', oi.value_usd ? '$' + formatLargeNumber(oi.value_usd) : '—');

  const badge = document.getElementById('badge-oi');
  if (badge) {
    const badgeInfo = change > 5 ? { text: 'Рост', cls: 'badge-bull' }
      : change < -5 ? { text: 'Снижение', cls: 'badge-bear' }
      : { text: 'Стабильно', cls: 'badge-neu' };
    badge.textContent = badgeInfo.text;
    badge.className = `badge ${badgeInfo.cls}`;
  }

  const bar = document.getElementById('bar-oi');
  if (bar) {
    const pct = clamp(50 + change * 3, 0, 100);
    bar.className = `bar-fill ${change > 0 ? 'bar-green' : change < 0 ? 'bar-red' : 'bar-orange'}`;
    bar.style.width = pct + '%';
  }
}

// ── Volume Profile ─────────────────────────────────
function renderVolumeProfile(vp, currentPrice) {
  setText('vp-poc', vp.poc ? '$' + formatPrice(vp.poc) : '—');
  setText('vp-vah', vp.vah ? '$' + formatPrice(vp.vah) : '—');
  setText('vp-val', vp.val ? '$' + formatPrice(vp.val) : '—');

  const container = document.getElementById('vp-histogram');
  if (!container || !vp.profile || vp.profile.length === 0) return;

  const maxPct = Math.max(...vp.profile.map(p => p.pct), 1);
  const pocPrice = vp.poc || 0;

  container.innerHTML = vp.profile.slice().reverse().map(bar => {
    const isPoc = Math.abs(bar.price - pocPrice) / pocPrice < 0.001;
    const inVA = bar.price >= (vp.val || 0) && bar.price <= (vp.vah || Infinity);
    let cls = 'vp-bar';
    if (isPoc) cls += ' vp-bar-poc';
    else if (inVA) cls += ' vp-bar-va';
    else cls += ' vp-bar-outer';

    return `<div class="${cls}" style="width:${bar.pct}%" title="$${formatPrice(bar.price)}: ${bar.volume}"></div>`;
  }).join('');
}

// ── Book Anomalies ─────────────────────────────────
function renderBookAnomalies(book) {
  const bidVol = book.total_bid || 0;
  const askVol = book.total_ask || 0;
  const total = bidVol + askVol;

  setText('book-bid-vol', bidVol > 1000 ? (bidVol / 1000).toFixed(1) + 'K' : bidVol.toFixed(1));
  setText('book-ask-vol', askVol > 1000 ? (askVol / 1000).toFixed(1) + 'K' : askVol.toFixed(1));
  setText('book-spread', book.spread_pct ? book.spread_pct.toFixed(3) + '%' : '—');

  const bidPct = total > 0 ? (bidVol / total * 100) : 50;
  const bidBar = document.getElementById('bar-book-bid');
  if (bidBar) {
    bidBar.style.width = bidPct + '%';
    bidBar.className = `bar-fill ${bidPct > 60 ? 'bar-green' : bidPct < 40 ? 'bar-red' : 'bar-orange'}`;
  }

  const badge = document.getElementById('badge-book');
  if (badge) {
    const imbalance = book.imbalance || 0;
    const absImb = Math.abs(imbalance);
    if (absImb > 0.3) {
      badge.textContent = imbalance > 0 ? 'Bid доминирует' : 'Ask доминирует';
      badge.className = `badge ${imbalance > 0 ? 'badge-bull' : 'badge-bear'}`;
    } else {
      badge.textContent = 'Баланс';
      badge.className = 'badge badge-neu';
    }
  }

  const list = document.getElementById('book-anomaly-list');
  if (!list) return;

  if (book.anomalies && book.anomalies.length > 0) {
    list.innerHTML = book.anomalies.slice(0, 3).map(a => `
      <div class="book-anomaly-item">
        <span class="book-anomaly-side ${a.side}">${a.side.toUpperCase()}</span>
        <span class="book-anomaly-price">$${formatPrice(a.price)}</span>
        <span class="book-anomaly-vol">${a.volume.toFixed(1)} (${a.ratio}x)</span>
      </div>
    `).join('');
  } else {
    list.innerHTML = '<div class="book-no-anomalies">Нет аномалий</div>';
  }
}

// ── Whale Activity ──────────────────────────────────
function playWhaleAlert(tier, direction) {
  if (!_whaleSoundEnabled) return;
  try {
    if (!_whaleAudioCtx) {
      _whaleAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    const ctx = _whaleAudioCtx;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.connect(gain);
    gain.connect(ctx.destination);

    // T3: двухтоновый сигнал (высокий-низкий), T2: один тон
    if (tier === 3) {
      osc.type = 'sine';
      osc.frequency.setValueAtTime(direction === 'buy' ? 880 : 660, ctx.currentTime);
      osc.frequency.setValueAtTime(direction === 'buy' ? 1100 : 880, ctx.currentTime + 0.12);
      gain.gain.setValueAtTime(0.25, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.35);
      osc.start(ctx.currentTime);
      osc.stop(ctx.currentTime + 0.35);
    } else {
      osc.type = 'sine';
      osc.frequency.setValueAtTime(660, ctx.currentTime);
      gain.gain.setValueAtTime(0.15, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.2);
      osc.start(ctx.currentTime);
      osc.stop(ctx.currentTime + 0.2);
    }
  } catch (e) {
    console.warn('Whale sound error:', e);
  }
}

function renderWhale(whale) {
  // Z-Score badge
  const badge = document.getElementById('whaleZscoreBadge');
  if (badge) {
    const z = whale.z_score_abs || 0;
    const zRaw = whale.z_score || 0;
    let label, cls;
    if (z >= 4)      { label = 'Экстремальная';  cls = 'whale-zscore-t3'; }
    else if (z >= 3) { label = 'Высокая';        cls = 'whale-zscore-t2'; }
    else if (z >= 2) { label = 'Повышенная';     cls = 'whale-zscore-t1'; }
    else if (z >= 0.5) { label = 'Умеренная';    cls = 'whale-zscore-normal'; }
    else               { label = 'Спокойно';      cls = 'whale-zscore-normal'; }
    badge.textContent = `${label} (${zRaw >= 0 ? '+' : ''}${zRaw.toFixed(2)})`;
    badge.className = `whale-zscore-badge ${cls}`;
    badge.title = `Текущий объём ${zRaw >= 0 ? 'выше' : 'ниже'} среднего за 20 баров`;
  }

  // VWAP
  setText('whaleVwap', whale.vwap ? '$' + formatPrice(whale.vwap) : '—');

  // Volume/SMA ratio
  const volRatioEl = document.getElementById('whaleVolRatio');
  if (volRatioEl) {
    if (whale.volume_sma > 0 && whale.volume > 0) {
      const ratio = (whale.volume / whale.volume_sma).toFixed(2);
      volRatioEl.textContent = ratio + 'x';
      volRatioEl.className = `whale-metric-value ${whale.volume > whale.volume_sma * 2 ? 'whale-dir-buy' : ''}`;
    } else {
      volRatioEl.textContent = '—';
    }
  }

  // T3 count
  setText('whaleT3Count', whale.tier3_count ?? 0);

  // Signals list (last 5)
  const list = document.getElementById('whaleSignalsList');
  if (!list) return;

  const signals = whale.signals || [];
  // Показываем только Tier 3 и Tier 2, последние 5
  const important = signals
    .filter(s => s.tier >= 2)
    .slice(-5);

  if (important.length === 0) {
    list.innerHTML = '<span class="whale-no-signals">Нет крупных аномалий</span>';
    return;
  }

  list.innerHTML = important.map(s => {
    const isBuy = s.direction === 'buy';
    const isInit = s.classification === 'INIT';

    // Маркер: T3 — большой, T2 — средний
    let marker;
    if (s.tier === 3) {
      marker = isInit
        ? `<span class="whale-marker whale-t3-init" title="Уровень 3 — Инициатива: крупный игрок толкает цену в сторону объёма">●</span>`
        : `<span class="whale-marker whale-t3-abs" title="Уровень 3 — Поглощение: объём упёрся в лимит, цена развернулась">✕</span>`;
    } else {
      marker = isBuy
        ? `<span class="whale-marker whale-t2-buy" title="Уровень 2 — Покупка">●</span>`
        : `<span class="whale-marker whale-t2-sell" title="Уровень 2 — Продажа">●</span>`;
    }

    const dirCls = isBuy ? 'whale-dir-buy' : 'whale-dir-sell';
    const clsLabel = isInit ? 'ИНИЦИАТИВА' : 'ПОГЛОЩЕНИЕ';
    const clsCls = isInit ? 'whale-cls-init' : 'whale-cls-abs';

    const ago = s.timestamp ? _timeAgo(s.timestamp) : '';
    return `
      <div class="whale-signal-item" title="Аномалия бара: Z-отклонение от среднего объёма">
        ${marker}
        <span class="${dirCls}">${isBuy ? 'ПОКУПКА' : 'ПРОДАЖА'}</span>
        <span class="${clsCls}">${clsLabel}</span>
        <span class="whale-signal-z" title="Насколько объём этого бара превышал норму">Z:${s.z_score?.toFixed(1) || '?'}${ago ? ' · ' + ago : ''}</span>
      </div>
    `;
  }).join('');

  // Звук при новом T3 сигнале
  const newT3 = important.filter(s => s.tier === 3 && !_lastWhaleSignals.has(s.bar_index));
  if (newT3.length > 0) {
    playWhaleAlert(3, newT3[0].direction);
  }
  // Обновляем множество seen signals
  important.forEach(s => _lastWhaleSignals.add(s.bar_index));
  // Очищаем старые, чтобы не рос бесконечно
  if (_lastWhaleSignals.size > 100) {
    const arr = [..._lastWhaleSignals].slice(-50);
    _lastWhaleSignals = new Set(arr);
  }
}

// ── Utils для нового функционала ───────────────────
function formatLargeNumber(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toFixed(0);
}

function formatPrice(p) {
  if (!p) return '0';
  let s;
  if (p >= 1000) {
    s = Number(p).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
  } else if (p >= 1) {
    s = p.toFixed(4);
  } else if (p >= 0.001) {
    s = p.toFixed(6);
  } else {
    s = p.toPrecision(4);
  }
  // Убираем trailing zeros после точки: 0.001900 → 0.0019
  if (s.includes('.')) {
    s = s.replace(/0+$/, '').replace(/\.$/, '');
  }
  return s;
}

// ── Token picker ────────────────────────────────────
document.getElementById('tokenBtn')?.addEventListener('click', () => {
  const raw = document.getElementById('tokenInput')?.value.trim().toUpperCase();
  if (raw) subscribeToToken(raw);
});

document.querySelectorAll('.qtok').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.qtok').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    subscribeToToken(btn.dataset.symbol);
  });
});

function subscribeToToken(symbol) {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: 'subscribe', symbol, timeframe: _currentTF }));
    showLoader(`Загрузка ${symbol}...`);
    setTimeout(hideLoader, 2000);
  }
}

// ── Utils ───────────────────────────────────────────
function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value ?? '—';
}

function clamp(v, min, max) {
  return Math.min(max, Math.max(min, v || 0));
}

function updateStatus(text) {
  const el = document.getElementById('statusText');
  if (el) el.innerHTML = text;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function showLoader(msg) {
  let overlay = document.getElementById('loadingOverlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'loadingOverlay';
    overlay.className = 'loading-overlay';
    overlay.innerHTML = `<div class="spinner"></div><span id="loaderMsg"></span>`;
    document.body.appendChild(overlay);
  }
  document.getElementById('loaderMsg').textContent = msg || 'Загрузка...';
  overlay.classList.add('visible');
}

function hideLoader() {
  document.getElementById('loadingOverlay')?.classList.remove('visible');
}


// ── Open Trades ──────────────────────────────────
function renderOpenTrades(trades) {
  const tbody = document.getElementById('tradesBody');
  const countEl = document.getElementById('tradesCount');
  if (!tbody) return;

  if (countEl) countEl.textContent = trades.length;

  if (!trades || trades.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="trades-empty">Нет открытых сделок</td></tr>';
    return;
  }

  tbody.innerHTML = trades.map((t, i) => {
    const signalCls = t.signal_type === 'BUY' ? 'trades-signal-buy' : 'trades-signal-sell';
    const sent = t.sent_at ? formatTradeTime(t.sent_at) : '—';
    return `<tr>
      <td>${i + 1}</td>
      <td class="${signalCls}">${t.signal_type}</td>
      <td class="trades-ticker">${escapeHtml(t.symbol)}</td>
      <td class="trades-tf">${escapeHtml(t.timeframe)}</td>
      <td class="trades-price">${formatPrice(t.entry)}</td>
      <td class="trades-price">${formatPrice(t.sl)}</td>
      <td class="trades-price">${formatPrice(t.tp)}</td>
      <td class="trades-sent">${sent}</td>
    </tr>`;
  }).join('');
}

function formatTradeTime(isoStr) {
  try {
    const d = new Date(isoStr);
    const day = d.getUTCDate().toString().padStart(2, '0');
    const month = (d.getUTCMonth() + 1).toString().padStart(2, '0');
    const hours = d.getUTCHours().toString().padStart(2, '0');
    const mins = d.getUTCMinutes().toString().padStart(2, '0');
    return `${day}.${month} ${hours}:${mins}`;
  } catch {
    return '—';
  }
}

async function fetchOpenTrades() {
  try {
    const res = await fetch('/api/open-trades');
    const data = await res.json();
    renderOpenTrades(data.trades || []);
  } catch (err) {
    console.error('Failed to load open trades:', err);
  }
}

// ── Start ──────────────────────────────────────────
connect();
fetchOpenTrades();

// ── Whale Sound Toggle ─────────────────────────────
document.getElementById('whaleSoundBtn')?.addEventListener('click', () => {
  _whaleSoundEnabled = !_whaleSoundEnabled;
  const btn = document.getElementById('whaleSoundBtn');
  if (btn) {
    btn.textContent = _whaleSoundEnabled ? '🔔' : '🔕';
    btn.classList.toggle('muted', !_whaleSoundEnabled);
  }
});

// ── Whale TF Toggle ────────────────────────────────
document.querySelectorAll('.whale-tf-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const tf = btn.dataset.tf;
    if (tf === _currentTF) return;
    _currentTF = tf;
    document.querySelectorAll('.whale-tf-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    // Re-subscribe with current symbol and new TF
    const activeTok = document.querySelector('.qtok.active');
    const symbol = activeTok?.dataset?.symbol || 'BTC';
    subscribeToToken(symbol);
  });
});
