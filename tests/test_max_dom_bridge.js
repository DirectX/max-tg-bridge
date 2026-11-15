// Тест JS-моста чтения MAX (app/max_dom.py:_JS_BRIDGE) на мини-DOM без браузера.
// Запуск из корня проекта:  bash tests/run_js_bridge_test.sh
// Проверяет: извлечение текста/автора, ключ data-message-id vs хэш-фолбэк,
// фото-без-текста (уникальные ключи по src), mine-маркеры, системные фильтры,
// фильтр аватарок, дедупликацию перерисовок, инкрементальный drain.
const fs = require('fs');
const BRIDGE_SRC = fs.readFileSync('.test_js_bridge.js', 'utf8');

class El {
  constructor(tag, cls = '', { text = '', attrs = {}, style = {} } = {}) {
    this.tagName = tag.toUpperCase(); this.className = cls;
    this.children = []; this.parentElement = null;
    this._text = text; this.attrs = attrs; this.style = style;
    this.src = ''; this.naturalWidth = 0; this.naturalHeight = 0;
  }
  get innerText() {
    return this.children.length ? this.children.map(c => c.innerText).join('\n') : this._text;
  }
  append(...kids) { for (const k of kids) { k.parentElement = this; this.children.push(k); } return this; }
  getAttribute(a) { return Object.prototype.hasOwnProperty.call(this.attrs, a) ? this.attrs[a] : null; }
  matches(sel) {
    const m = sel.trim().match(/^([a-z0-9]+)?((?:\.[\w-]+)*)$/i);
    if (!m) return false;
    if (m[1] && m[1].toUpperCase() !== this.tagName) return false;
    const cls = (this.className || '').split(/\s+/);
    for (const c of m[2].split('.').filter(Boolean)) if (!cls.includes(c)) return false;
    return true;
  }
  querySelectorAll(sel) {
    const parts = String(sel).split(',').map(s => s.trim()).filter(Boolean);
    const out = [];
    (function walk(n) {
      for (const c of n.children) { if (parts.some(p => c.matches(p))) out.push(c); walk(c); }
    })(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

// ---------- «чат» ----------
const doc = new El('#document');
const body = new El('body'); doc.append(body);
const chat = new El('div', 'chat-body');
function msg(cls, id, author, text, opts = {}) {
  const box = new El('div', cls);
  if (id) box.attrs['data-message-id'] = id;
  if (author !== null) box.append(new El('div', 'author', { text: author }));
  if (text !== null) box.append(new El('div', 'msg-text', { text }));
  box.append(new El('time', '', { text: opts.time || '12:34' }));
  return box;
}
const m1 = msg('message', 'id-1', 'Мария', 'Не задали ДЗ');
const m2 = msg('message-out', null, 'Иван Петрович', 'Спасибо!');   // исходящее
const m3 = new El('div', 'message');                                 // только картинка
const img = new El('img', 'photo'); img.src = 'blob:https://web.max.ru/abc';
img.naturalWidth = img.naturalHeight = 400; m3.append(img);
const m3b = new El('div', 'message');                                // ещё фото без текста
const img2 = new El('img', 'photo'); img2.src = 'blob:https://web.max.ru/def';
img2.naturalWidth = img2.naturalHeight = 400; m3b.append(img2);
const sys = msg('message', null, '', 'Иван вступил в чат');          // системное
const dup = msg('message', null, 'Мария', 'Не задали ДЗ');           // дубль m1 по хэшу
const avatarWrap = new El('div', 'avatar');
const av = new El('img', 'photo'); av.src = 'https://max.ru/a.png';
av.naturalWidth = av.naturalHeight = 40; avatarWrap.append(av);
chat.append(m1, m2, m3, m3b, sys, dup, avatarWrap);
body.append(chat);

global.window = global;
global.document = doc;
global.Notification = { permission: 'default' };
global.MutationObserver = class { constructor() {} observe() {} disconnect() {} };

eval(BRIDGE_SRC);

const cfg = {
  scope: ['div.chat-body'], text: ['div.msg-text'], author: ['div.author'],
  images: ['img.photo'], meta: ['time'], typing: [],
  idAttrs: ['data-message-id'], mineMarkers: ['message-out'],
  systemMarkers: ['вступил'], selfName: '',
};
window.__MaxBridge.install(cfg);

setTimeout(() => {
  const q = window.__MaxBridge.drain();
  const fail = [];
  const byText = {};
  for (const it of q) byText[it.text] = it;

  if (q.length !== 5) fail.push(`ожидал 5 сообщений, получил ${q.length}: ` +
    q.map(x => JSON.stringify({ t: x.text, m: x.mine, imgs: x.imgs.length })).join(' | '));
  if (!q.find(x => x.key === 'data-message-id=id-1')) fail.push('m1 ключ не data-id');
  const m2q = byText['Спасибо!'];
  if (!m2q || !m2q.mine) fail.push('m2 не помечено mine');
  const photoOnly = q.filter(x => x.imgs.length === 1 && !x.text);
  if (photoOnly.length !== 2) fail.push('фото-без-текста: ' + photoOnly.length + ' из 2');
  if (new Set(photoOnly.map(x => x.key)).size !== 2) fail.push('ключи фото-без-текста совпали');
  if (byText['Иван вступил в чат']) fail.push('системное не отфильтровано');
  if (q.filter(x => x.text === 'Не задали ДЗ').length !== 2) fail.push('потеря Повтор сообщения');
  if (q.some(x => x.imgs.some(i => i.src.includes('a.png')))) fail.push('аватарка утекла в images');

  window.__MaxBridge.seed();
  chat.append(msg('message', 'id-5', 'Пётр', 'Собрание в пятницу'));
  chat.append(msg('message', null, 'Мария', 'Не задали ДЗ', { time: '12:34' }));
  // m1 ещё раз в DOM-е (перерисовка) -> пережит seed, повтором не придёт
  window.__MaxBridge.schedule();
  setTimeout(() => {
    const q2 = window.__MaxBridge.drain();
    if (q2.length !== 1 || q2[0].text !== 'Собрание в пятницу')
      fail.push('инкрементальный drain: ' + JSON.stringify(q2.map(x => ({t: x.text, k: x.key}))));
    console.log('stats:', JSON.stringify(window.__MaxBridge.stats()));
    if (fail.length) { console.log('JS BRIDGE TEST FAIL:\n - ' + fail.join('\n - ')); process.exit(1); }
    console.log('JS BRIDGE TEST PASS');
    process.exit(0);
  }, 400);
}, 400);
