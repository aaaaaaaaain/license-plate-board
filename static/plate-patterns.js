// 號碼型態篩選（鐵支／豹子／順子／對子／回文）——看板和歷史查詢共用這一份，
// 兩邊的下拉選單都是從 PLATE_PATTERNS 生出來的，加一種型態只要改這裡。
//
// 一律只看號牌結尾的那串數字（跟 extract_plate_number、numberKeyOf 同一條規則），
// 字首字母不算進型態：大家講的「8888」指的是數字，換字首重新上架還是同一個型態。

function plateDigits(plate) {
  const m = String(plate == null ? '' : plate).match(/(\d+)\s*$/);
  return m ? m[1] : '';
}

// 連續重複的最長長度：8880 是 3、8888 是 4、1234 是 1
function longestRun(d) {
  let best = 1, run = 1;
  for (let i = 1; i < d.length; i++) {
    run = d[i] === d[i - 1] ? run + 1 : 1;
    if (run > best) best = run;
  }
  return d.length ? best : 0;
}

// 整串是不是等差 ±1（1234 遞增、9876 遞減）。沒有跨 9→0 接回去，
// 9012 這種在市場上算不算順子見仁見智，這裡採嚴格定義。
function isRun(d, step) {
  if (d.length < 3) return false;
  for (let i = 1; i < d.length; i++) {
    if (Number(d[i]) - Number(d[i - 1]) !== step) return false;
  }
  return true;
}

const PLATE_PATTERNS = [
  {
    value: 'quad',
    label: '鐵支（四同，如 8888）',
    test: d => d.length >= 4 && longestRun(d) >= 4,
  },
  {
    value: 'triple',
    label: '豹子（三同，如 1888）',
    // 四同已經有自己的選項，這裡只留剛好三個的，兩個選項才不會互相蓋掉
    test: d => longestRun(d) === 3,
  },
  {
    value: 'straight',
    label: '順子（如 1234／9876）',
    test: d => isRun(d, 1) || isRun(d, -1),
  },
  {
    value: 'pairs',
    label: '對子（如 1122／1212）',
    // AABB 與 ABAB 兩種，兩對必須是不同數字，否則 8888 也會被算成對子
    test: d => d.length === 4 && (
      (d[0] === d[1] && d[2] === d[3] && d[0] !== d[2]) ||
      (d[0] === d[2] && d[1] === d[3] && d[0] !== d[1])
    ),
  },
  {
    value: 'mirror',
    label: '回文（如 1221）',
    test: d => d.length === 4 && d[0] === d[3] && d[1] === d[2] && d[0] !== d[1],
  },
];

function matchesPlatePattern(plate, value) {
  if (!value) return true;
  const pattern = PLATE_PATTERNS.find(p => p.value === value);
  if (!pattern) return true;
  return pattern.test(plateDigits(plate));
}

// 把型態選項灌進指定的 <select>，第一個永遠是「全部型態」（不篩）
function fillPatternSelect(select) {
  if (!select) return;
  select.innerHTML = '<option value="">全部號碼型態</option>';
  for (const p of PLATE_PATTERNS) {
    const opt = document.createElement('option');
    opt.value = p.value;
    opt.textContent = p.label;
    select.appendChild(opt);
  }
}
