// 配色切換：自動（跟系統）／淺色／深色，選擇記在 localStorage，每個頁面共用。
//
// 這支要放在 <head> 裡、而且不能加 defer——它必須在頁面畫出來之前就把
// data-theme 設好，不然深色模式的使用者每次換頁都會先閃一下白底。
//
// CSS 那邊只認 :root[data-theme="dark"]，「自動」是在這裡解析成 light/dark 後
// 才寫進去的，所以深色的那組變數在 style.css 裡只需要寫一份。
(function () {
  const KEY = 'theme-preference';
  const MODES = ['auto', 'light', 'dark'];
  const LABELS = { auto: '🌓 自動', light: '☀️ 淺色', dark: '🌙 深色' };
  // 狀態列要跟頁面最上面的顏色一樣：深色的 header 是 rgba(20,20,22,.82) 疊在黑底上，
  // 實際是 #101012。頁面底色不一樣的（登入頁）在 <html> 上用 data-bar-light／
  // data-bar-dark 自己指定。
  const BAR_COLOR = { light: '#F2F2F7', dark: '#101012' };
  const media = window.matchMedia('(prefers-color-scheme: dark)');

  // iOS Safari 預設不套用 :active（要頁面上有 touchstart 監聽才會），再加上
  // style.css 把點擊高亮設成透明，iPhone 上按任何按鈕都會完全沒有反應。
  // 空的監聽器就夠了；passive 不會擋到捲動。
  document.addEventListener('touchstart', function () {}, { passive: true });

  // 切換深淺色時整頁顏色淡過去（.theme-transition 在 style.css），
  // 不要瞬間整片翻白／翻黑。只在「切換」時掛，第一次畫面不需要。
  let fadeTimer = null;
  function applyWithFade(pref) {
    const root = document.documentElement;
    root.classList.add('theme-transition');
    apply(pref);
    clearTimeout(fadeTimer);
    fadeTimer = setTimeout(function () { root.classList.remove('theme-transition'); }, 300);
  }

  function read() {
    try {
      const v = localStorage.getItem(KEY);
      return MODES.indexOf(v) >= 0 ? v : 'auto';
    } catch (e) {
      return 'auto';  // 無痕模式擋掉 localStorage 時就當作自動，不要整支壞掉
    }
  }

  function write(pref) {
    try { localStorage.setItem(KEY, pref); } catch (e) { /* 存不了就只有這次有效 */ }
  }

  function apply(pref) {
    const mode = pref === 'auto' ? (media.matches ? 'dark' : 'light') : pref;
    document.documentElement.dataset.theme = mode;
    // 手機瀏覽器的網址列／狀態列顏色。原本兩個 meta 各自綁 prefers-color-scheme，
    // 手動指定配色時那兩個還是會跟著系統走，顏色就對不上了，統一改成由這裡設定。
    const metas = document.querySelectorAll('meta[name="theme-color"]');
    metas.forEach(function (m, i) {
      if (i === 0) {
        m.removeAttribute('media');
        const own = mode === 'dark' ? document.documentElement.dataset.barDark
          : document.documentElement.dataset.barLight;
        m.setAttribute('content', own || BAR_COLOR[mode]);
      } else {
        m.remove();
      }
    });
    const btn = document.getElementById('themeToggle');
    if (btn) {
      btn.textContent = LABELS[pref];
      btn.title = '目前：' + LABELS[pref].slice(2) + '（點一下切換）';
    }
  }

  window.cycleTheme = function () {
    const next = MODES[(MODES.indexOf(read()) + 1) % MODES.length];
    write(next);
    applyWithFade(next);
  };

  apply(read());

  // 停在「自動」時，系統在深淺之間切換要跟著變（例如 iOS 的日落自動深色）
  media.addEventListener('change', function () {
    if (read() === 'auto') applyWithFade('auto');
  });

  document.addEventListener('DOMContentLoaded', function () {
    apply(read());
    const btn = document.getElementById('themeToggle');
    if (btn) btn.addEventListener('click', window.cycleTheme);
  });
})();
