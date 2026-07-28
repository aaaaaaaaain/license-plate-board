// 密碼欄位的「顯示/隱藏」切換：把 <input type="password"> 包在 <div class="pw-field">
// 裡、旁邊放一個 class="pw-toggle" 的按鈕，這支就會自動接上點擊事件。
function initPasswordToggles() {
  document.querySelectorAll('.pw-toggle').forEach(btn => {
    const input = btn.previousElementSibling;
    if (!input) return;
    btn.addEventListener('click', () => {
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      btn.textContent = showing ? '顯示' : '隱藏';
    });
  });
}
document.addEventListener('DOMContentLoaded', initPasswordToggles);
