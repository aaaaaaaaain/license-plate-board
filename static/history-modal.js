// 共用的「出價歷史」彈出視窗 + 走勢圖邏輯，index.html 跟 history.html 都會用到。
// 使用前頁面裡要有 #historyModal / #historyTitle / #historyChart / #historyTableWrap / #historyClose 這些元素。

function extractPlateNumber(plate) {
  const m = plate.match(/(\d+)\s*$/);
  return m ? m[1] : plate;
}

// 目前彈窗顯示的是哪個號碼／監理站，截圖時要把這些資訊一起畫進圖片裡
let currentHistoryContext = null;

function renderHistoryChart(history, decided) {
  const container = document.getElementById('historyChart');
  if (history.length === 0) {
    container.innerHTML = '<div class="hint" style="text-align:center;padding:24px 0;">尚無歷史紀錄</div>';
    return;
  }
  const width = 320, height = 160;
  const padding = { top: 22, right: 14, bottom: 22, left: 44 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;

  const prices = history.map(h => Number(h.price) || 0);
  const minP = Math.min(...prices);
  const maxP = Math.max(...prices);
  // 只有一筆紀錄、或每一筆出價都一樣時，maxP-minP 會是 0。這時候如果只回退成 1，
  // 五條格線會擠成幾乎一樣的數字（例如 3000/3000/3001/3001/3001），看起來像壞掉——
  // 而且大部分號碼從被追蹤到現在都還沒有人加價，剛好都是這種情況。
  // 改用價格量級的一成當範圍，並把這個唯一的價格擺在圖的正中間（不然點會黏在底線上）。
  const flat = maxP === minP;
  const rangeP = flat ? Math.max(Math.round(maxP * 0.1), 100) : maxP - minP;
  const baseP = flat ? minP - rangeP / 2 : minP;
  const n = history.length;

  const x = i => padding.left + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = v => padding.top + plotH - ((v - baseP) / rangeP) * plotH;

  const steps = 4;
  let grid = '';
  for (let s = 0; s <= steps; s++) {
    const val = baseP + (rangeP * s / steps);
    const yy = y(val);
    grid += `<line x1="${padding.left}" y1="${yy}" x2="${width - padding.right}" y2="${yy}" stroke="var(--separator)" stroke-width="1"/>`;
    grid += `<text x="${padding.left - 6}" y="${yy + 3}" font-size="9" text-anchor="end" fill="var(--label-secondary)">${Math.round(val)}</text>`;
  }

  const points = history.map((h, i) => `${x(i)},${y(Number(h.price) || 0)}`).join(' ');
  const lastIdx = n - 1;
  const dots = history.map((h, i) => {
    const cx = x(i), cy = y(Number(h.price) || 0);
    const isDecidedPoint = decided && i === lastIdx;
    const color = isDecidedPoint ? 'var(--green, #34C759)' : 'var(--accent)';
    const tip = isDecidedPoint
      ? `${h.recorded_at.replace('T', ' ')}\n${h.plate}\n✅ 已決標：${h.price} 元（第 ${h.bid_count} 次）`
      : `${h.recorded_at.replace('T', ' ')}\n${h.plate}\n${h.price} 元（第 ${h.bid_count} 次）`;
    return `<circle cx="${cx}" cy="${cy}" r="4" fill="${color}" stroke="var(--card-bg)" stroke-width="1.5">` +
      `<title>${tip}</title></circle>`;
  }).join('');

  const last = history[history.length - 1];
  const lastX = x(n - 1), lastY = y(Number(last.price) || 0);
  const labelY = Math.max(lastY - 10, 10);
  const labelColor = decided ? 'var(--green, #34C759)' : 'var(--accent)';
  const labelText = decided ? `✅ ${last.price} 元` : `${last.price} 元`;
  const label = `<text x="${lastX}" y="${labelY}" font-size="11" font-weight="600" text-anchor="end" fill="${labelColor}">${labelText}</text>`;

  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" style="width:100%;height:auto;display:block;overflow:visible;">
      ${grid}
      <polyline points="${points}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      ${dots}
      ${label}
    </svg>
  `;
}

function renderHistoryTable(history) {
  const wrap = document.getElementById('historyTableWrap');
  if (history.length === 0) {
    wrap.innerHTML = '';
    return;
  }
  const rows = history.slice().reverse().map(h => `
    <tr>
      <td>${h.recorded_at.replace('T', ' ')}</td>
      <td>${h.plate}</td>
      <td>${h.price} 元</td>
      <td>第 ${h.bid_count} 次</td>
    </tr>
  `).join('');
  wrap.innerHTML = `
    <table>
      <thead><tr><th>時間</th><th>號牌</th><th>出價</th><th>次數</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderDecidedBanner(decided) {
  const el = document.getElementById('historyDecidedBanner');
  if (!el) return;
  if (!decided) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = 'block';
  el.innerHTML = `
    ✅ 已決標：<strong>${decided.final_price} 元</strong>
    <div class="hint" style="margin-top:2px;">決標時間 ${decided.decided_at ? decided.decided_at.replace('T', ' ') : '-'}</div>
  `;
}

async function openHistory(plate, category, section, station) {
  currentHistoryContext = { plate, category, section, station };
  document.getElementById('historyTitle').textContent = `號碼 ${extractPlateNumber(plate)}（${category}）`;
  document.getElementById('historyChart').innerHTML = '<div class="hint" style="text-align:center;padding:24px 0;">載入中...</div>';
  document.getElementById('historyTableWrap').innerHTML = '';
  renderDecidedBanner(null);
  document.getElementById('historyModal').classList.add('show');
  try {
    // 有帶 section/station 就一起送，避免不同監理站剛好尾數相同的號碼被誤連成同一條趨勢線
    let url = `/api/history/${encodeURIComponent(plate)}?category=${encodeURIComponent(category)}`;
    if (section) url += `&section=${encodeURIComponent(section)}`;
    if (station) url += `&station=${encodeURIComponent(station)}`;
    const resp = await fetch(url);
    const data = await resp.json();
    currentHistoryContext.history = data.history || [];
    currentHistoryContext.decided = data.decided;
    renderHistoryChart(data.history || [], data.decided);
    renderHistoryTable(data.history || []);
    renderDecidedBanner(data.decided);
  } catch (e) {
    document.getElementById('historyChart').innerHTML = '<div class="hint" style="text-align:center;padding:24px 0;">載入失敗</div>';
  }
}

function closeHistory() {
  document.getElementById('historyModal').classList.remove('show');
}

// 把目前顯示的走勢圖存成一張 PNG 圖片下載，圖片裡另外畫上號碼、車種、
// 轄區、監理站，這樣圖片單獨拿出去分享時還是看得出來是哪個號碼、在哪個監理站。
//
// SVG 圖表本身用的顏色是 var(--accent) 這類 CSS 變數，直接把 SVG 拿去餵給
// <img> 當獨立圖片載入的話，會脫離網頁本身的 CSS 環境、讀不到這些變數，
// 所以要先把每個節點目前「實際算出來的顏色」讀出來、寫死回節點上，
// 才能匯出成一張顏色正確的獨立圖片。
async function downloadHistoryScreenshot() {
  const svg = document.querySelector('#historyChart svg');
  if (!svg || !currentHistoryContext) return;
  const btn = document.getElementById('historyScreenshotBtn');
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '產生圖片中...';

  try {
    const { plate, category, section, station, history } = currentHistoryContext;
    const viewBox = svg.viewBox.baseVal;
    const chartW = viewBox && viewBox.width ? viewBox.width : 320;
    const chartH = viewBox && viewBox.height ? viewBox.height : 160;

    const clone = svg.cloneNode(true);
    clone.removeAttribute('style');
    clone.setAttribute('width', chartW);
    clone.setAttribute('height', chartH);
    const origNodes = [svg, ...svg.querySelectorAll('*')];
    const cloneNodes = [clone, ...clone.querySelectorAll('*')];
    origNodes.forEach((orig, i) => {
      const cl = cloneNodes[i];
      if (orig.getAttribute('stroke')) cl.setAttribute('stroke', getComputedStyle(orig).stroke);
      if (orig.getAttribute('fill')) cl.setAttribute('fill', getComputedStyle(orig).fill);
    });

    const svgData = new XMLSerializer().serializeToString(clone);
    const svgBlob = new Blob([svgData], { type: 'image/svg+xml;charset=utf-8' });
    const svgUrl = URL.createObjectURL(svgBlob);
    const img = new Image();
    await new Promise((resolve, reject) => {
      img.onload = resolve;
      img.onerror = reject;
      img.src = svgUrl;
    });
    URL.revokeObjectURL(svgUrl);

    // 下面的出價紀錄表格照畫面上的順序（最新一筆在最上面）
    const rows = (history || []).slice().reverse();
    const headerH = 46;
    const tableHeaderH = 22;
    const tableRowH = 20;
    const tableTop = headerH + chartH + 12;
    const tableH = rows.length > 0 ? tableHeaderH + rows.length * tableRowH + 8 : 0;
    const canvasW = Math.max(chartW, 380);  // 表格四欄比圖表寬，畫布另外抓寬一點才不會擠
    const canvasH = tableTop + tableH;

    const scale = 2;  // 匯出解析度加倍，文字線條比較不會糊
    const canvas = document.createElement('canvas');
    canvas.width = canvasW * scale;
    canvas.height = canvasH * scale;
    const ctx = canvas.getContext('2d');
    ctx.scale(scale, scale);

    const isDark = matchMedia('(prefers-color-scheme: dark)').matches;
    const colorBg = isDark ? '#1c1c1e' : '#ffffff';
    const colorLabel = isDark ? '#ffffff' : '#1c1c1e';
    const colorSecondary = isDark ? '#8e8e93' : '#6d6d72';
    const colorAccent = isDark ? '#0a84ff' : '#007aff';
    const colorSeparator = isDark ? 'rgba(255,255,255,.15)' : 'rgba(0,0,0,.1)';
    const fontStack = '"PingFang TC", "Microsoft JhengHei", -apple-system, sans-serif';

    ctx.fillStyle = colorBg;
    ctx.fillRect(0, 0, canvasW, canvasH);

    ctx.fillStyle = colorLabel;
    ctx.font = `600 14px ${fontStack}`;
    ctx.fillText(`號碼 ${extractPlateNumber(plate)}（${category}）`, 12, 20);
    ctx.fillStyle = colorSecondary;
    ctx.font = `12px ${fontStack}`;
    ctx.fillText(`${section || ''} ${station || ''}`.trim(), 12, 38);

    ctx.drawImage(img, 0, headerH, chartW, chartH);

    if (rows.length > 0) {
      const cols = [
        { label: '時間', x: 10, get: r => (r.recorded_at || '').replace('T', ' ') },
        { label: '號牌', x: 155, get: r => r.plate || '' },
        { label: '出價', x: 228, get: r => `${r.price} 元` },
        { label: '次數', x: 305, get: r => `第 ${r.bid_count} 次` },
      ];
      let y = tableTop;
      ctx.font = `600 10px ${fontStack}`;
      ctx.fillStyle = colorAccent;
      for (const col of cols) ctx.fillText(col.label, col.x, y + 14);
      y += tableHeaderH;

      ctx.font = `10px ${fontStack}`;
      for (const r of rows) {
        ctx.strokeStyle = colorSeparator;
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(canvasW, y);
        ctx.stroke();
        ctx.fillStyle = colorLabel;
        for (const col of cols) ctx.fillText(col.get(r), col.x, y + 14);
        y += tableRowH;
      }
    }

    showScreenshotPreview(
      canvas.toDataURL('image/png'),
      `車牌趨勢_${extractPlateNumber(plate)}_${station || ''}.png`
    );
  } catch (e) {
    console.error('產生截圖失敗', e);
    alert('產生截圖失敗，請再試一次');
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

// iOS Safari 不支援 <a download> 觸發下載（點了通常什麼事都不會發生），尤其是
// 上面那段又有 await 過圖片載入完成，早就不算是使用者操作當下同一個事件了，
// Safari 更不可能放行。改成把產生好的圖片直接顯示在頁面上的預覽框，
// 手機長按圖片存到相簿、電腦右鍵另存新檔，任何瀏覽器都能用同一套做法存檔。
function ensureScreenshotPreviewOverlay() {
  let overlay = document.getElementById('screenshotPreviewOverlay');
  if (overlay) return overlay;
  overlay = document.createElement('div');
  overlay.id = 'screenshotPreviewOverlay';
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal-sheet">
      <div class="modal-header">
        <strong>截圖預覽</strong>
        <button type="button" id="screenshotPreviewClose" class="modal-close" aria-label="關閉">✕</button>
      </div>
      <div class="hint" style="text-align:center;margin-bottom:8px;">
        手機請長按圖片選「儲存圖片」；電腦可以右鍵另存新檔
      </div>
      <img id="screenshotPreviewImg" alt="趨勢圖截圖" style="width:100%;border-radius:10px;display:block;">
      <a id="screenshotPreviewDownload" class="ios-btn-block primary" style="display:block;text-align:center;text-decoration:none;margin-top:10px;">⬇️ 下載圖片</a>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', (e) => {
    if (e.target.id === 'screenshotPreviewOverlay') overlay.classList.remove('show');
  });
  document.getElementById('screenshotPreviewClose').addEventListener('click', () => {
    overlay.classList.remove('show');
  });
  return overlay;
}

function showScreenshotPreview(dataUrl, filename) {
  const overlay = ensureScreenshotPreviewOverlay();
  document.getElementById('screenshotPreviewImg').src = dataUrl;
  const dlLink = document.getElementById('screenshotPreviewDownload');
  dlLink.href = dataUrl;
  dlLink.download = filename;
  overlay.classList.add('show');
}

function initHistoryModal() {
  document.getElementById('historyClose').addEventListener('click', closeHistory);
  document.getElementById('historyModal').addEventListener('click', (e) => {
    if (e.target.id === 'historyModal') closeHistory();
  });
  const screenshotBtn = document.getElementById('historyScreenshotBtn');
  if (screenshotBtn) screenshotBtn.addEventListener('click', downloadHistoryScreenshot);
}
