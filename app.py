import base64
import hashlib
import html as htmllib
import mimetypes
import os
import random
import re
import shutil
import tempfile
import time
from html.parser import HTMLParser
from urllib.parse import unquote

import streamlit as st

from anki.collection import Collection
from anki import sync_pb2
from anki.utils import ids2str

CR = sync_pb2.SyncCollectionResponse.ChangesRequired

st.set_page_config(page_title="Anki 카드 보기", layout="wide")

# ----------------------------------------------------------------------------
# HTML 정리 (카드 내용은 사용자 데이터이므로 화면에 넣기 전에 반드시 걸러낸다)
# ----------------------------------------------------------------------------

VOID = {"br", "hr", "img"}
ALLOWED = {
    "b", "strong", "i", "em", "u", "s", "strike", "del", "mark", "sub", "sup",
    "br", "hr", "div", "span", "p", "ul", "ol", "li", "table", "thead", "tbody",
    "tfoot", "tr", "td", "th", "img", "font", "code", "pre", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "small", "big", "center",
}
DROP = {
    "script", "style", "iframe", "object", "embed", "noscript", "template", "svg",
    "form", "input", "button", "textarea", "select", "audio", "video", "link",
    "meta", "base", "title", "head",
}
BAD_PROPS = {"position", "z-index", "top", "left", "right", "bottom", "behavior",
             "-moz-binding", "filter"}


def clean_style(s):
    out = []
    for decl in s.split(";"):
        if ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        prop, v = prop.strip().lower(), val.strip().lower()
        if prop in BAD_PROPS or "url(" in v or "expression" in v \
                or "javascript" in v or "@import" in v or "\\" in v:
            continue
        out.append(f"{prop}:{val.strip()}")
    return ";".join(out)


class _Sanitizer(HTMLParser):
    def __init__(self, media, edit):
        super().__init__(convert_charrefs=True)
        self.media = media
        self.edit = edit
        self.out = []
        self.stack = []
        self.skip = 0

    def _attrs(self, tag, attrs):
        res = []
        for k, v in attrs:
            k = k.lower()
            v = v or ""
            if k == "class" and re.fullmatch(r"[\w\- ]*", v):
                res.append(("class", v))
            elif k == "style":
                cs = clean_style(v)
                if cs:
                    res.append(("style", cs))
            elif tag in ("td", "th") and k in ("colspan", "rowspan") and v.isdigit():
                res.append((k, v))
            elif tag == "font" and k == "color" and re.fullmatch(r"[#\w(),. %]*", v):
                res.append((k, v))
            elif tag == "img" and k in ("alt", "width", "height") and len(v) < 200:
                res.append((k, v))
            elif tag == "img" and k == "src":
                low = v.strip().lower()
                if low.startswith("data:image/") or low.startswith("https://"):
                    res.append(("src", v))
                elif not re.match(r"^[a-z][a-z0-9+.\-]*:", low):
                    name = os.path.basename(unquote(v))
                    uri = self.media(name)
                    if uri:
                        res.append(("src", uri))
                        if self.edit:
                            res.append(("data-orig", name))
                    else:
                        res.append(("alt", f"[이미지: {name}]"))
        return res

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag in DROP and tag not in VOID:
                self.skip += 1
            return
        if tag in DROP:
            if tag not in VOID:
                self.skip = 1
            return
        if tag not in ALLOWED:
            return
        a = "".join(f' {k}="{htmllib.escape(v, quote=True)}"' for k, v in self._attrs(tag, attrs))
        self.out.append(f"<{tag}{a}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in DROP and tag not in VOID:
                self.skip -= 1
            return
        if tag in ALLOWED and tag not in VOID and tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(htmllib.escape(data, quote=False))

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize(raw, media, edit=False):
    p = _Sanitizer(media, edit)
    p.feed(raw or "")
    p.close()
    return p.result()


def media_resolver(col):
    cache = st.session_state.setdefault("media_cache", {})
    base = col.media.dir()

    def resolve(name):
        if name in cache:
            return cache[name]
        uri = None
        path = os.path.join(base, name)
        mime = mimetypes.guess_type(name)[0] or ""
        if mime.startswith("image/") and os.path.isfile(path) and os.path.getsize(path) <= 3_000_000:
            with open(path, "rb") as f:
                uri = f"data:{mime};base64," + base64.b64encode(f.read()).decode()
        cache[name] = uri
        return uri

    return resolve


# ----------------------------------------------------------------------------
# 컴포넌트: 목록(편집 가능) / 카드 격자(뒤집기)
# ----------------------------------------------------------------------------

LIST_HTML = """
<div class="root">
  <div class="top">
    <button class="mode">편집 모드 켜기</button>
    <button class="add-top">+ 맨 위에 추가</button>
    <button class="add">+ 맨 아래에 추가</button>
    <span class="info"></span>
  </div>
  <div class="list"></div>
  <div class="sentinel"></div>
</div>
"""

LIST_CSS = """
.root { font-size: 15px; line-height: 1.5; }
.top { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 0 0 8px 0; }
button {
  font: inherit; font-size: 13px; padding: 2px 8px; cursor: pointer;
  background: transparent; color: inherit;
  border: 1px solid var(--st-border-color, #c8c8c8); border-radius: 3px;
}
.add, .add-top { display: none; }
.root.editing .add, .root.editing .add-top { display: inline-block; }
.root:not(.editing) .ins { display: none; }
.tb button.on { border-color: var(--st-primary-color, #4a78b8); box-shadow: inset 0 0 0 1px var(--st-primary-color, #4a78b8); }
.tb button.on:not(.dot)::after { content: " ✓"; font-size: 11px; }
.tb .dot { line-height: 14px; text-align: center; }
.tb .dot.on::after { content: "✓"; font-size: 12px; font-weight: bold; color: #222; }
.sentinel { height: 1px; }
.info { font-size: 13px; opacity: .75; }
.list { display: flex; flex-direction: column; gap: 8px; }
.row {
  border: 1px solid var(--st-border-color, #c8c8c8); border-left-width: 4px;
  border-radius: 3px; padding: 4px 10px 8px;
}
.row.s-new { border-left-color: #4a78b8; }
.row.s-learn { border-left-color: #d08a2e; }
.row.s-review { border-left-color: #4f9a5a; }
.row.s-susp { border-left-color: #999; }
.row.s-add { border-left-color: #8a5ab8; }
.row.deleted { opacity: .45; }
.row.deleted .cols, .row.deleted .extra { text-decoration: line-through; }
.head { display: flex; align-items: center; gap: 10px; font-size: 12px; min-height: 28px; }
.head .no { font-weight: bold; }
.head .deck { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; opacity: .7; }
.head .acts { display: flex; gap: 4px; margin-left: auto; }
.acts button { padding: 0 6px; font-size: 12px; }
.root:not(.editing) .del { display: none; }
.tb { display: none; align-items: center; gap: 4px; margin: 0 auto; }
.root.editing .row:focus-within .tb { display: flex; }
.root.editing .row:focus-within .deck { display: none; }
.tb button { padding: 0 7px; }
.tb .lbl { font-size: 11px; opacity: .7; margin-left: 4px; }
.tb .dot { width: 18px; height: 18px; border-radius: 50%; padding: 0; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.ed {
  min-height: 44px; padding: 6px 8px; outline: none; overflow-wrap: anywhere;
  border-bottom: 2px solid transparent; border-radius: 2px;
  background: var(--st-secondary-background-color, #f3f3f3);
}
.root:not(.editing) .ed { background: transparent; padding-left: 0; }
.root.editing .ed:focus { border-bottom-color: var(--st-primary-color, #4a78b8); }
.ed:empty:before { content: attr(data-ph); opacity: .4; }
.ed img { max-width: 100%; height: auto; }
.cap { font-size: 12px; opacity: .6; margin-top: 2px; }
.more { margin-top: 6px; }
.extra { display: none; margin-top: 6px; }
.extra.open { display: block; }
.extra .fld { margin-bottom: 6px; }
.empty { padding: 20px 0; opacity: .7; }
@media (max-width: 640px) { .cols { grid-template-columns: 1fr; } }
"""

LIST_JS = r"""export default function(component) {
  const { data, setStateValue, setTriggerValue, parentElement } = component;
  if (parentElement.__sig === data.sig) return;
  parentElement.__sig = data.sig;

  const root = parentElement.querySelector('.root');
  const list = parentElement.querySelector('.list');
  const sentinel = parentElement.querySelector('.sentinel');
  const modeBtn = parentElement.querySelector('.mode');
  const addBtn = parentElement.querySelector('.add');
  const addTopBtn = parentElement.querySelector('.add-top');
  const info = parentElement.querySelector('.info');
  const S = { edits: {}, adds: [], dels: [] };
  const eds = [];
  let edit = false;
  let timer = null;
  let rendered = 0;
  let observer = null;
  let addCount = 0;
  const BATCH = 40;

  const norm = (c) => {
    if (!c) return '';
    const t = document.createElement('span');
    t.style.color = c;
    return t.style.color;
  };
  const hasContent = (a) => a.fields.some((f) => f.replace(/<(?!img)[^>]*>/g, '').trim() !== '');
  const syncAdds = () => {
    let last = null;
    const arr = [];
    list.querySelectorAll(':scope > .row').forEach((r) => {
      if (r._nid !== undefined) last = r._nid;
      else if (r._add) { r._add.after = last; arr.push(r._add); }
    });
    S.adds = arr;
  };
  const refresh = () => {
    const n = Object.keys(S.edits).length, a = S.adds.filter(hasContent).length, d = S.dels.length;
    info.textContent = (n || a || d) ? ('저장 전 변경: 수정 ' + n + ' · 추가 ' + a + ' · 삭제 ' + d) : '';
  };
  const emit = () => {
    syncAdds();
    refresh();
    clearTimeout(timer);
    timer = setTimeout(() => setStateValue('changes', JSON.parse(JSON.stringify(S))), 300);
  };
  const clean = (el) => {
    const c = el.cloneNode(true);
    c.querySelectorAll('img[data-orig]').forEach((i) => {
      i.setAttribute('src', i.dataset.orig);
      i.removeAttribute('data-orig');
    });
    return c.innerHTML
      .replace(/<div><br\s*\/?><\/div>/gi, '<br>')
      .replace(/<div>/gi, '<br>')
      .replace(/<\/div>/gi, '')
      .replace(/&nbsp;/g, ' ');
  };

  const TB =
    '<button data-cmd="bold" title="굵게"><b>B</b></button>' +
    '<button data-cmd="italic" title="기울임"><i>I</i></button>' +
    '<button data-cmd="underline" title="밑줄"><u>U</u></button>' +
    '<span class="lbl">형광펜</span>' +
    '<button class="dot" data-cmd="hiliteColor" data-val="#ffe94d" style="background:#ffe94d" title="노랑 형광펜"></button>' +
    '<button class="dot" data-cmd="hiliteColor" data-val="#a8d4ff" style="background:#a8d4ff" title="파랑 형광펜"></button>' +
    '<button class="dot" data-cmd="hiliteColor" data-val="#f5b8f0" style="background:#f5b8f0" title="분홍 형광펜"></button>' +
    '<span class="lbl">글자색</span>' +
    '<button class="dot" data-cmd="foreColor" data-val="#d03030" style="background:#d03030" title="빨강"></button>' +
    '<button class="dot" data-cmd="foreColor" data-val="#2f6fd0" style="background:#2f6fd0" title="파랑"></button>' +
    '<button class="dot" data-cmd="foreColor" data-val="#2a8a3e" style="background:#2a8a3e" title="초록"></button>' +
    '<button data-act="img" title="사진 추가 (붙여넣기도 가능)">사진</button>' +
    '<input class="file" type="file" accept="image/*" style="display:none">' +
    '<button data-cmd="removeFormat" title="서식 지우기">서식 지우기</button>';

  const getSel = () => (parentElement.getSelection ? parentElement.getSelection() : window.getSelection());
  const saveRange = () => {
    const sel = getSel();
    return sel && sel.rangeCount ? sel.getRangeAt(0).cloneRange() : null;
  };
  const readAsDataUri = (file) => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = reject;
    r.readAsDataURL(file);
  });
  const fileToDataUri = (file) => new Promise((resolve, reject) => {
    if (file.type === 'image/gif' && file.size < 1500000) { readAsDataUri(file).then(resolve, reject); return; }
    const url = URL.createObjectURL(file);
    const im = new Image();
    im.onload = () => {
      const MAX = 1200;
      const k = Math.min(1, MAX / Math.max(im.naturalWidth, im.naturalHeight));
      const w = Math.max(1, Math.round(im.naturalWidth * k));
      const h = Math.max(1, Math.round(im.naturalHeight * k));
      const draw = (white) => {
        const cv = document.createElement('canvas');
        cv.width = w; cv.height = h;
        const ctx = cv.getContext('2d');
        if (white) { ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, w, h); }
        ctx.drawImage(im, 0, 0, w, h);
        return cv;
      };
      let out;
      if (file.type === 'image/png') {
        out = draw(false).toDataURL('image/png');
        if (out.length > 1500000) out = draw(true).toDataURL('image/jpeg', 0.8);
      } else {
        out = draw(true).toDataURL('image/jpeg', 0.85);
        if (out.length > 1500000) out = draw(true).toDataURL('image/jpeg', 0.7);
      }
      URL.revokeObjectURL(url);
      resolve(out);
    };
    im.onerror = () => { URL.revokeObjectURL(url); reject(new Error('image')); };
    im.src = url;
  });
  const insertImg = (ed, uri, range) => {
    const img = document.createElement('img');
    img.src = uri;
    if (range && ed.contains(range.startContainer)) {
      range.deleteContents();
      range.insertNode(img);
      range.setStartAfter(img);
      range.collapse(true);
    } else {
      ed.appendChild(img);
    }
    ed.dispatchEvent(new Event('input'));
  };

  // 현재 커서/선택 위치의 서식을 막대 버튼에 체크로 표시
  const updateTb = () => {
    const a = parentElement.activeElement;
    if (!a || !a.classList || !a.classList.contains('ed')) return;
    const row = a.closest('.row');
    if (!row) return;
    const qs = (c) => { try { return document.queryCommandState(c); } catch (e) { return false; } };
    const qv = (c) => { try { return document.queryCommandValue(c); } catch (e) { return ''; } };
    const hl = norm(qv('backColor'));
    const fc = norm(qv('foreColor'));
    row.querySelectorAll('.tb button[data-cmd]').forEach((b) => {
      const cmd = b.dataset.cmd;
      let on = false;
      if (cmd === 'bold' || cmd === 'italic' || cmd === 'underline') on = qs(cmd);
      else if (cmd === 'hiliteColor') on = !!hl && hl === norm(b.dataset.val);
      else if (cmd === 'foreColor') on = !!fc && fc === norm(b.dataset.val);
      b.classList.toggle('on', !!on);
    });
  };
  const stripDefaults = (ed) => {
    const defC = norm(getComputedStyle(ed).color);
    ed.querySelectorAll('[style]').forEach((el) => {
      if (el.style.color && norm(el.style.color) === defC) el.style.removeProperty('color');
      const bg = el.style.backgroundColor;
      if (bg && (bg === 'transparent' || /,\s*0\)$/.test(bg))) el.style.removeProperty('background-color');
      if (!el.getAttribute('style')) el.removeAttribute('style');
      if (el.tagName === 'SPAN' && !el.attributes.length) el.replaceWith(...el.childNodes);
    });
  };
  document.addEventListener('selectionchange', updateTb);

  const makeCell = (html, name, onChange) => {
    const wrap = document.createElement('div');
    const ed = document.createElement('div');
    ed.className = 'ed';
    ed.dataset.ph = name;
    ed.innerHTML = html;
    ed.contentEditable = edit ? 'true' : 'false';
    ed.addEventListener('input', () => onChange(clean(ed)));
    ed.addEventListener('keyup', updateTb);
    ed.addEventListener('mouseup', updateTb);
    ed.addEventListener('paste', (e) => {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for (const it of items) {
        if (it.kind === 'file' && it.type.startsWith('image/')) {
          e.preventDefault();
          const f = it.getAsFile();
          const range = saveRange();
          fileToDataUri(f).then((uri) => insertImg(ed, uri, range)).catch(() => {});
          return;
        }
      }
    });
    const cap = document.createElement('div');
    cap.className = 'cap';
    cap.textContent = name;
    wrap.appendChild(ed);
    wrap.appendChild(cap);
    eds.push(ed);
    return wrap;
  };

  const bindToolbar = (row) => {
    const tb = row.querySelector('.tb');
    const fileInput = tb.querySelector('.file');
    let pendingImg = null;
    fileInput.onchange = () => {
      const f = fileInput.files[0];
      fileInput.value = '';
      if (!f || !pendingImg) return;
      const { ed, range } = pendingImg;
      pendingImg = null;
      fileToDataUri(f).then((uri) => insertImg(ed, uri, range)).catch(() => {});
    };
    tb.addEventListener('mousedown', (e) => e.preventDefault());
    tb.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      const a = parentElement.activeElement;
      if (!a || !a.classList || !a.classList.contains('ed')) return;
      if (b.dataset.act === 'img') {
        pendingImg = { ed: a, range: saveRange() };
        fileInput.click();
        return;
      }
      const cmd = b.dataset.cmd;
      if (cmd === 'hiliteColor' || cmd === 'foreColor') {
        const wasOn = b.classList.contains('on');
        document.execCommand('styleWithCSS', false, true);
        if (wasOn && cmd === 'hiliteColor') document.execCommand('hiliteColor', false, 'transparent');
        else if (wasOn) document.execCommand('foreColor', false, getComputedStyle(a).color);
        else document.execCommand(cmd, false, b.dataset.val);
        if (wasOn) stripDefaults(a);
      } else {
        document.execCommand('styleWithCSS', false, false);
        document.execCommand(cmd, false, null);
      }
      a.dispatchEvent(new Event('input'));
      updateTb();
    });
  };

  const buildRow = (opts) => {
    // opts: {no, stateKey, stateLabel, deck, marked, nid, fields, onField, onDelete, isAdd}
    const row = document.createElement('div');
    row.className = 'row s-' + opts.stateKey;
    row.innerHTML =
      '<div class="head"><span class="no">' + opts.no + '</span>' +
      '<span class="state">' + opts.stateLabel + '</span>' +
      '<span class="deck">' + (opts.deck || '') + '</span>' +
      '<span class="tb">' + TB + '</span>' +
      '<span class="acts">' + (opts.isAdd ? '' : '<button class="star" title="별표">' + (opts.marked ? '★' : '☆') + '</button>') +
      '<button class="ins" title="이 줄 아래에 새 카드 추가">아래에 추가</button>' +
      '<button class="del">삭제</button></span></div>' +
      '<div class="cols"></div><div class="extra"></div>';
    const cols = row.querySelector('.cols');
    const extra = row.querySelector('.extra');
    opts.fields.forEach((f, i) => {
      const cell = makeCell(f.html, f.name, (h) => opts.onField(i, h));
      if (i < 2) cols.appendChild(cell);
      else { cell.className = 'fld'; extra.appendChild(cell); }
    });
    if (opts.fields.length > 2) {
      const more = document.createElement('button');
      more.className = 'more';
      more.textContent = '필드 ' + (opts.fields.length - 2) + '개 더 보기';
      more.onclick = () => {
        extra.classList.toggle('open');
        more.textContent = extra.classList.contains('open') ? '필드 접기' : '필드 ' + (opts.fields.length - 2) + '개 더 보기';
      };
      row.insertBefore(more, extra);
    }
    bindToolbar(row);
    row.querySelector('.del').onclick = () => opts.onDelete(row);
    row.querySelector('.ins').onclick = () => addRowAt(row);
    const star = row.querySelector('.star');
    if (star) star.onclick = () => {
      opts.marked = !opts.marked;
      star.textContent = opts.marked ? '★' : '☆';
      setTriggerValue('star', { nid: opts.nid, on: opts.marked });
    };
    return row;
  };

  const makeExistingRow = (r, i) => {
    const row = buildRow({
      no: data.offset + i + 1, nid: r.nid, stateKey: r.state_key, stateLabel: r.state,
      deck: r.deck, marked: r.marked, fields: r.fields,
      onField: (idx, h) => {
        (S.edits[r.nid] = S.edits[r.nid] || {})[idx] = h;
        emit();
      },
      onDelete: (rowEl) => {
        const k = S.dels.indexOf(r.nid);
        if (k >= 0) { S.dels.splice(k, 1); rowEl.classList.remove('deleted'); rowEl.querySelector('.del').textContent = '삭제'; }
        else { S.dels.push(r.nid); rowEl.classList.add('deleted'); rowEl.querySelector('.del').textContent = '되돌리기'; }
        emit();
      },
    });
    row._nid = r.nid;
    return row;
  };

  const renderMore = (n) => {
    const end = Math.min(data.rows.length, rendered + n);
    for (; rendered < end; rendered++) list.appendChild(makeExistingRow(data.rows[rendered], rendered));
    if (rendered >= data.rows.length && observer) { observer.disconnect(); observer = null; }
  };
  const renderAll = () => renderMore(data.rows.length);

  // pos: 'top' | 'end' | 기준 줄(그 아래에 삽입)
  function addRowAt(pos) {
    renderAll();
    const a = { fields: data.new_names.map(() => ''), after: null };
    addCount += 1;
    const row = buildRow({
      no: '새 카드 ' + addCount, stateKey: 'add', stateLabel: '추가 예정', deck: data.new_deck,
      isAdd: true,
      fields: data.new_names.map((n) => ({ name: n, html: '' })),
      onField: (idx, h) => { a.fields[idx] = h; emit(); },
      onDelete: (rowEl) => { rowEl.remove(); emit(); },
    });
    row._add = a;
    if (pos === 'top') list.prepend(row);
    else if (pos === 'end') list.appendChild(row);
    else pos.after(row);
    const first = row.querySelector('.ed');
    if (first) first.focus();
    emit();
  }

  if (!data.rows.length) {
    list.innerHTML = '<div class="empty">조건에 맞는 카드가 없습니다.</div>';
  }
  if ('IntersectionObserver' in window) {
    observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        renderMore(BATCH);
        if (observer) { observer.unobserve(sentinel); observer.observe(sentinel); }
      }
    }, { rootMargin: '800px' });
    observer.observe(sentinel);
  } else {
    renderAll();
  }
  renderMore(BATCH);

  addBtn.onclick = () => addRowAt('end');
  addTopBtn.onclick = () => addRowAt('top');
  modeBtn.onclick = () => {
    edit = !edit;
    root.classList.toggle('editing', edit);
    modeBtn.textContent = edit ? '편집 모드 끄기' : '편집 모드 켜기';
    eds.forEach((e) => { e.contentEditable = edit ? 'true' : 'false'; });
  };
  refresh();

  return () => {
    document.removeEventListener('selectionchange', updateTb);
    if (observer) observer.disconnect();
  };
}
"""

GRID_HTML = """
<div class="bar">
  <button class="all-front">모두 앞면</button>
  <button class="all-back">모두 뒷면</button>
</div>
<div class="grid"></div>
<div class="sentinel"></div>
"""

GRID_CSS = """
.bar { margin: 0 0 8px 0; }
.bar button, .acts button {
  font: inherit; font-size: 13px; padding: 2px 8px; cursor: pointer;
  background: transparent; color: inherit;
  border: 1px solid var(--st-border-color, #c8c8c8); border-radius: 3px;
}
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 8px; }
.card {
  border: 1px solid var(--st-border-color, #c8c8c8); border-left-width: 4px;
  border-radius: 3px; padding: 8px 10px; min-height: 150px; cursor: pointer;
  display: flex; flex-direction: column; font-size: 15px; line-height: 1.5;
  overflow-wrap: anywhere;
}
.card.s-new { border-left-color: #4a78b8; }
.card.s-learn { border-left-color: #d08a2e; }
.card.s-review { border-left-color: #4f9a5a; }
.card.s-susp { border-left-color: #999; }
.meta { display: flex; align-items: center; gap: 8px; font-size: 12px; opacity: .75; margin-bottom: 6px; }
.meta .deck { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.acts { display: flex; gap: 4px; }
.acts button { padding: 0 6px; font-size: 12px; }
.face { flex: 1; text-align: left; }
.face img { max-width: 100%; height: auto; }
.back { display: none; }
.card.flipped .front { display: none; }
.card.flipped .back { display: block; }
.card.both .front { display: block; }
.card.both .back { display: block; border-top: 1px solid var(--st-border-color, #c8c8c8); margin-top: 6px; padding-top: 6px; }
.empty { padding: 20px 0; opacity: .7; }
.cloze { font-weight: bold; color: #2f6fd0; }
.sentinel { height: 1px; }
"""

GRID_JS = r"""export default function(component) {
  const { data, setTriggerValue, parentElement } = component;
  const root = parentElement.querySelector('.grid');
  const sentinel = parentElement.querySelector('.sentinel');
  const flipped = parentElement.__flipped || (parentElement.__flipped = new Set());
  root.innerHTML = '';
  if (!data.cards.length) {
    root.innerHTML = '<div class="empty">조건에 맞는 카드가 없습니다.</div>';
  }
  const BATCH = 60;
  const els = [];
  let rendered = 0;
  let observer = null;
  const makeCard = (c) => {
    const el = document.createElement('div');
    el.className = 'card s-' + c.state_key + (data.both ? ' both' : '');
    if (!data.both && flipped.has(c.id)) el.classList.add('flipped');
    el.innerHTML =
      '<div class="meta"><span>' + c.state + '</span><span class="deck">' + c.deck + '</span>' +
      '<span class="acts"><button class="star" title="별표">' + (c.marked ? '★' : '☆') + '</button></span></div>' +
      '<div class="face front">' + c.front + '</div>' +
      '<div class="face back">' + c.back + '</div>';
    el.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b && b.classList.contains('star')) {
        c.marked = !c.marked;
        b.textContent = c.marked ? '★' : '☆';
        setTriggerValue('star', { nid: c.nid, on: c.marked });
        return;
      }
      if (data.both) return;
      el.classList.toggle('flipped');
      if (el.classList.contains('flipped')) flipped.add(c.id); else flipped.delete(c.id);
    });
    return el;
  };
  const renderMore = (n) => {
    const end = Math.min(data.cards.length, rendered + n);
    for (; rendered < end; rendered++) {
      const el = makeCard(data.cards[rendered]);
      root.appendChild(el);
      els.push([data.cards[rendered], el]);
    }
    if (rendered >= data.cards.length && observer) { observer.disconnect(); observer = null; }
  };
  if ('IntersectionObserver' in window) {
    observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        renderMore(BATCH);
        if (observer) { observer.unobserve(sentinel); observer.observe(sentinel); }
      }
    }, { rootMargin: '800px' });
    observer.observe(sentinel);
  } else {
    renderMore(data.cards.length);
  }
  renderMore(BATCH);
  const setAll = (on) => {
    data.cards.forEach((c) => { if (on) flipped.add(c.id); else flipped.delete(c.id); });
    els.forEach(([c, el]) => el.classList.toggle('flipped', on));
  };
  parentElement.querySelector('.all-front').onclick = () => setAll(false);
  parentElement.querySelector('.all-back').onclick = () => setAll(true);
  return () => { if (observer) observer.disconnect(); };
}
"""

list_component = st.components.v2.component(
    "anki_note_list", html=LIST_HTML, css=LIST_CSS, js=LIST_JS
)
grid_component = st.components.v2.component(
    "anki_card_grid", html=GRID_HTML, css=GRID_CSS, js=GRID_JS
)

# ----------------------------------------------------------------------------
# AnkiWeb 로그인 / 다운로드 / 동기화
# ----------------------------------------------------------------------------

STATE_QUERY = {
    "새 카드": "is:new",
    "학습 중": "is:learn",
    "복습": "is:review",
    "보류": "is:suspended",
    "별표": "tag:marked",
}


def close_session():
    col = st.session_state.pop("col", None)
    if col is not None:
        try:
            col.close()
        except Exception:
            pass
    d = st.session_state.pop("workdir", None)
    if d:
        shutil.rmtree(d, ignore_errors=True)
    for k in ("auth", "dirty", "media_cache", "media_dirty", "ver", "grid_key", "seed", "backup", "flash",
              "note_cache", "card_cache"):
        st.session_state.pop(k, None)


def login_and_download(user, password, with_media, status):
    close_session()
    workdir = tempfile.mkdtemp(prefix="ankiweb_")
    st.session_state.workdir = workdir
    col = Collection(os.path.join(workdir, "collection.anki2"))
    status.write("로그인 중...")
    auth = col.sync_login(user, password, None)
    out = col.sync_collection(auth, False)
    if out.new_endpoint:
        auth.endpoint = out.new_endpoint
    if out.required == CR.FULL_UPLOAD:
        col.close()
        raise RuntimeError("AnkiWeb에 데이터가 없습니다. Anki 프로그램에서 먼저 동기화해 주세요.")
    status.write("컬렉션 내려받는 중...")
    col.close_for_full_sync()
    col.full_upload_or_download(auth=auth, server_usn=None, upload=False)
    col.reopen(after_full_sync=True)
    if with_media:
        status.write("이미지 파일 내려받는 중... (처음에는 오래 걸릴 수 있습니다)")
        col.sync_media(auth)
        for _ in range(900):
            s = col.media_sync_status()
            if not s.active:
                break
            time.sleep(1)
    st.session_state.col = col
    st.session_state.auth = auth
    st.session_state.dirty = set()
    st.session_state.ver = 0


def sync_back():
    col, auth = st.session_state.col, st.session_state.auth
    out = col.sync_collection(auth, False)
    if out.new_endpoint:
        auth.endpoint = out.new_endpoint
    if out.required in (CR.FULL_SYNC, CR.FULL_DOWNLOAD, CR.FULL_UPLOAD):
        return False, (
            "AnkiWeb와 전체 동기화가 필요한 상태입니다. 데이터를 덮어쓸 수 있어서 "
            "이 앱에서는 진행하지 않습니다. Anki 프로그램에서 동기화 방향을 선택해 해결해 주세요."
        )
    st.session_state.dirty = set()
    bump_ver()
    st.session_state.media_cache = {}
    msg = out.server_message or "동기화 완료"
    if st.session_state.get("media_dirty"):
        try:
            col.sync_media(auth)
            for _ in range(900):
                if not col.media_sync_status().active:
                    break
                time.sleep(1)
            st.session_state.media_dirty = False
            msg += " (사진도 함께 올렸습니다)"
        except Exception as e:
            return False, f"카드는 동기화됐지만 사진 업로드에 실패했습니다: {e}"
    return True, msg


def make_backup():
    col = st.session_state.col
    path = os.path.join(st.session_state.workdir, "backup.colpkg")
    col.export_collection_package(path, include_media=False, legacy=True)
    with open(path, "rb") as f:
        return f.read()


# ----------------------------------------------------------------------------
# 카드 읽기 / 저장
# ----------------------------------------------------------------------------

def esc_search(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("*", "\\*").replace("_", "\\_")


def build_query(decks, states, text):
    parts = []
    if decks:
        parts.append("(" + " or ".join(f'"deck:{esc_search(d)}"' for d in decks) + ")")
    if states:
        parts.append("(" + " or ".join(STATE_QUERY[s] for s in states) + ")")
    if text.strip():
        parts.append(text.strip())
    return " ".join(parts)


AV_RE = re.compile(r"\[anki:[^\]]*\]")
ANSWER_SPLIT = re.compile(r"<hr id=[\"']?answer[\"']?\s*/?>", re.I)


def card_state(card):
    if card.queue == -1:
        return "보류", "susp"
    if card.type == 0:
        return "새 카드", "new"
    if card.type in (1, 3):
        return "학습 중", "learn"
    return "복습", "review"


def deck_name(col, did, cache):
    if did not in cache:
        cache[did] = col.decks.name(did)
    return cache[did]


def card_payload(col, cid, media, deck_names):
    card = col.get_card(cid)
    note = card.note()
    q = card.question()
    a = card.answer()
    parts = ANSWER_SPLIT.split(a, maxsplit=1)
    back = parts[1] if len(parts) == 2 else a
    label, key = card_state(card)
    return {
        "id": int(cid),
        "nid": int(note.id),
        "front": sanitize(AV_RE.sub("🔊", q), media),
        "back": sanitize(AV_RE.sub("🔊", back), media),
        "state": label,
        "state_key": key,
        "deck": htmllib.escape(deck_name(col, card.did, deck_names)),
        "marked": "marked" in [t.lower() for t in note.tags],
    }


def note_payload(col, nid, media, deck_names):
    note = col.get_note(nid)
    cards = sorted(note.cards(), key=lambda c: c.ord)
    label, key = card_state(cards[0]) if cards else ("", "new")
    deck = deck_name(col, cards[0].did, deck_names) if cards else ""
    return {
        "nid": int(nid),
        "state": label,
        "state_key": key,
        "deck": htmllib.escape(deck),
        "marked": "marked" in [t.lower() for t in note.tags],
        "fields": [
            {"name": n, "html": sanitize(note[n], media, edit=True)} for n in note.keys()
        ],
    }


def toggle_star(nid, on):
    col = st.session_state.col
    note = col.get_note(nid)
    if on:
        note.add_tag("marked")
    else:
        note.remove_tag("marked")
    col.update_note(note)
    st.session_state.dirty.add(nid)
    c = st.session_state.get("note_cache", {}).get(nid)
    if c:
        c["marked"] = bool(on)
    for v in st.session_state.get("card_cache", {}).values():
        if v["nid"] == nid:
            v["marked"] = bool(on)


def bump_ver():
    st.session_state.ver = st.session_state.get("ver", 0) + 1
    st.session_state.pop("note_cache", None)
    st.session_state.pop("card_cache", None)


def cached_note_payload(col, nid, media, deck_names):
    cache = st.session_state.setdefault("note_cache", {})
    if nid not in cache:
        cache[nid] = note_payload(col, nid, media, deck_names)
    return cache[nid]


def cached_card_payload(col, cid, media, deck_names):
    cache = st.session_state.setdefault("card_cache", {})
    if cid not in cache:
        cache[cid] = card_payload(col, cid, media, deck_names)
    return cache[cid]


def sort_notes_by_position(col, nids):
    """새 카드 위치(학습 순서)대로 정렬. 새 카드가 아닌 노트는 만든 순으로 뒤에 모은다."""
    if not nids:
        return nids
    first = {}
    for nid, ord_, typ, due in col.db.all(
        f"select nid, ord, type, due from cards where nid in {ids2str(nids)}"
    ):
        if nid not in first or ord_ < first[nid][0]:
            first[nid] = (ord_, typ, due)

    def key(n):
        _, typ, due = first.get(n, (0, 2, 0))
        return (0, due, n) if typ == 0 else (1, 0, n)

    return sorted(nids, key=key)


def place_added_cards(col, added):
    """추가한 카드를 화면에서 끼워 넣은 위치(위 카드의 다음 학습 순서)에 배치한다.

    added: [(카드 id 목록, 위에 있던 기존 노트 id 또는 None), ...] — 화면 순서대로.
    새 카드(학습 전)인 노트 아래에만 배치할 수 있고, 그렇지 않은 경우의 개수를 돌려준다.
    """
    unplaced = 0
    prev_after, prev_last = object(), None
    for cids, after in added:
        start = None
        count_unplaced = False
        if prev_last is not None and after == prev_after:
            start = col.get_card(prev_last).due + 1
        elif after is None:
            mn = col.db.scalar(
                f"select min(due) from cards where type = 0 and id not in {ids2str(cids)}"
            )
            start = mn
        else:
            mx = col.db.scalar(
                "select max(due) from cards where nid = ? and type = 0", int(after)
            )
            start = (mx + 1) if mx is not None else None
            count_unplaced = start is None
        if start is None:
            if count_unplaced:
                unplaced += 1
            prev_after, prev_last = after, (cids[-1] if after is None else None)
            continue
        col.sched.reposition_new_cards(
            cids, starting_from=int(start), step_size=1, randomize=False, shift_existing=True
        )
        prev_after, prev_last = after, cids[-1]
    return unplaced


def _get(res, name):
    if res is None:
        return None
    try:
        v = getattr(res, name)
    except Exception:
        try:
            v = res[name]
        except Exception:
            return None
    return v


def _plain(v):
    if hasattr(v, "items"):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def on_star_event():
    res = st.session_state.get(st.session_state.get("grid_key", ""))
    star = _plain(_get(res, "star"))
    if star:
        toggle_star(int(star["nid"]), bool(star["on"]))


IMG_RE = re.compile(r"<img", re.I)
TAG_RE = re.compile(r"<[^>]*>")


def has_content(fields):
    for f in fields:
        if IMG_RE.search(f or "") or TAG_RE.sub("", f or "").strip():
            return True
    return False


DATA_IMG_RE = re.compile(r'src="data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=]+)"')


def materialize_images(col, html):
    """편집기에서 넣은 사진(data URI)을 미디어 폴더 파일로 저장하고 파일명으로 바꾼다."""

    def repl(m):
        ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
        try:
            data = base64.b64decode(m.group(2))
        except Exception:
            return m.group(0)
        name = f"paste-{hashlib.sha1(data).hexdigest()[:16]}.{ext}"
        fname = col.media.write_data(name, data)
        st.session_state.media_dirty = True
        st.session_state.setdefault("media_cache", {}).pop(fname, None)
        return f'src="{fname}"'

    return DATA_IMG_RE.sub(repl, html or "")


def apply_changes(col, ch, deck_id, model_id):
    edits = ch.get("edits") or {}
    adds = ch.get("adds") or []
    dels = [int(x) for x in (ch.get("dels") or [])]
    dirty = st.session_state.dirty
    n_edit = n_add = n_del = 0
    errors = []

    for nid_s, fields in edits.items():
        nid = int(nid_s)
        if nid in dels:
            continue
        try:
            note = col.get_note(nid)
            names = note.keys()
            changed = False
            for idx_s, html in fields.items():
                i = int(idx_s)
                html = materialize_images(col, html)
                if i < len(names) and note.fields[i] != html:
                    note.fields[i] = html
                    changed = True
            if changed:
                col.update_note(note)
                dirty.add(nid)
                n_edit += 1
        except Exception as e:
            errors.append(f"수정 실패: {e}")

    model = col.models.get(model_id)
    added = []
    for a in adds:
        flds = a.get("fields") or []
        if not has_content(flds):
            continue
        try:
            note = col.new_note(model)
            for i, f in enumerate(flds[: len(note.fields)]):
                note.fields[i] = materialize_images(col, f)
            col.add_note(note, deck_id)
            dirty.add(int(note.id))
            n_add += 1
            cids = sorted(note.card_ids(), key=lambda c: col.get_card(c).ord)
            after = a.get("after")
            added.append((cids, int(after) if after is not None else None))
        except Exception as e:
            errors.append(f"추가 실패: {e}")

    unplaced = 0
    if added:
        try:
            unplaced = place_added_cards(col, added)
        except Exception as e:
            errors.append(f"카드 위치 지정 실패: {e}")

    if dels:
        try:
            col.remove_notes(dels)
            dirty.update(dels)
            n_del = len(dels)
        except Exception as e:
            errors.append(f"삭제 실패: {e}")

    bump_ver()
    return n_edit, n_add, n_del, errors, unplaced


# ----------------------------------------------------------------------------
# 화면
# ----------------------------------------------------------------------------

st.title("Anki 카드 보기")

col = st.session_state.get("col")

if col is None:
    st.write("AnkiWeb 계정으로 로그인하면 덱을 내려받아 여러 카드를 한 화면에서 보고 고칠 수 있습니다.")
    with st.form("login"):
        user = st.text_input("AnkiWeb 이메일")
        pw = st.text_input("비밀번호", type="password")
        with_media = st.checkbox("이미지도 함께 내려받기 (처음에는 오래 걸릴 수 있음)", value=False)
        go = st.form_submit_button("불러오기")
    st.caption(
        "비밀번호는 로그인에만 쓰이고 저장되지 않습니다. 내려받은 컬렉션은 이 세션 동안만 서버에 "
        "임시로 보관되며, 로그아웃하거나 세션이 끝나면 삭제됩니다."
    )
    if go:
        if not user or not pw:
            st.error("이메일과 비밀번호를 입력해 주세요.")
        else:
            with st.status("AnkiWeb에서 불러오는 중", expanded=True) as status:
                try:
                    login_and_download(user, pw, with_media, status)
                    status.update(label="불러오기 완료", state="complete")
                except Exception as e:
                    close_session()
                    status.update(label="실패", state="error")
                    st.error(f"불러오지 못했습니다: {e}")
                    st.stop()
            st.rerun()
    st.stop()

media = media_resolver(col)

flash = st.session_state.pop("flash", None)
if flash:
    (st.success if flash[0] == "ok" else st.warning)(flash[1])

# ---- 사이드바: 필터 / 새 카드 위치 / 동기화
with st.sidebar:
    st.subheader("보기")
    view_mode = st.radio("보기 방식", ["목록 (편집 가능)", "카드 (뒤집기)"])
    deck_list = sorted(d.name for d in col.decks.all_names_and_ids())
    sel_decks = st.multiselect("덱", deck_list)
    sel_states = st.multiselect("카드 상태", list(STATE_QUERY.keys()))
    text = st.text_input("검색 (Anki 검색 문법 가능)")
    sort_mode = st.selectbox(
        "정렬 (목록)", ["만든 순", "학습 순서 (새 카드 위치)"],
        disabled=view_mode.startswith("카드"),
        help="'아래에 추가'로 끼워 넣은 카드는 학습 순서로 정렬해야 넣은 자리에 보입니다. "
             "이미 복습 중인 카드는 순서가 없어서 새 카드 뒤에 모아 보여줍니다.",
    )
    both = st.checkbox("앞면과 뒷면을 함께 보기", disabled=view_mode.startswith("목록"))
    shuffle = st.checkbox("섞어서 보기")
    if shuffle and st.button("다시 섞기"):
        st.session_state.seed = random.random()

    new_deck_name = new_model_name = None
    if view_mode.startswith("목록"):
        st.divider()
        st.subheader("새 카드 추가 위치")
        default_deck = (
            sel_decks[0] if len(sel_decks) == 1
            else next((d for d in deck_list if d != "Default"), deck_list[0])
        )
        new_deck_name = st.selectbox("덱 ", deck_list, index=deck_list.index(default_deck))
        model_names = sorted(m.name for m in col.models.all_names_and_ids())
        default_model = next(
            (m for m in model_names if m in ("Basic", "기본")), model_names[0]
        )
        new_model_name = st.selectbox("노트 유형", model_names, index=model_names.index(default_model))
        st.caption("덱이나 노트 유형을 바꾸면 저장하지 않은 변경이 사라집니다.")

    st.divider()
    st.subheader("AnkiWeb 동기화")
    dirty = st.session_state.get("dirty", set())
    st.write(f"이 세션에서 변경한 노트: {len(dirty)}개")
    if st.button("백업 파일 만들기"):
        with st.spinner("만드는 중..."):
            st.session_state.backup = make_backup()
    if st.session_state.get("backup"):
        st.download_button("백업 내려받기 (.colpkg)", st.session_state.backup,
                           file_name="anki_backup.colpkg")
    if st.session_state.get("media_dirty"):
        st.caption("새 사진이 있어 이미지도 함께 동기화합니다. 처음에는 오래 걸릴 수 있어요.")
    confirm = st.checkbox(f"변경한 {len(dirty)}개 노트를 AnkiWeb에 반영합니다", disabled=not dirty)
    if st.button("AnkiWeb에 동기화", disabled=not (dirty and confirm), type="primary"):
        with st.spinner("동기화 중..."):
            try:
                ok, msg = sync_back()
            except Exception as e:
                ok, msg = False, f"동기화에 실패했습니다: {e}"
        (st.success if ok else st.error)(msg)
    st.divider()
    if st.button("로그아웃 (임시 데이터 삭제)"):
        close_session()
        st.rerun()

# ---- 검색 결과
query = build_query(sel_decks, sel_states, text)
list_mode = view_mode.startswith("목록")
try:
    ids = list(col.find_notes(query)) if list_mode else list(col.find_cards(query))
except Exception as e:
    st.error(f"검색어를 해석하지 못했습니다: {e}")
    st.stop()

if list_mode:
    # 노트 id는 만든 시각(ms)이므로 정렬하면 만든 순이 된다
    ids = sorted(ids)
    if sort_mode.startswith("학습"):
        ids = sort_notes_by_position(col, ids)
if shuffle:
    random.Random(st.session_state.get("seed", 0)).shuffle(ids)

counts = {s: len(col.find_cards(q)) for s, q in STATE_QUERY.items()}
unit = "노트" if list_mode else "카드"
st.caption(
    f"전체 카드 {len(col.find_cards(''))}장 · " + " · ".join(f"{s} {n}" for s, n in counts.items())
    + f" · 검색 결과 {unit} {len(ids)}개 (한 페이지에 모두 표시)"
)
if len(ids) > 1500:
    st.info("카드가 많아서 처음 불러올 때 오래 걸릴 수 있어요. 덱이나 검색으로 범위를 줄이면 빨라집니다.")

deck_names = {}
ver = st.session_state.get("ver", 0)

if list_mode:
    new_deck_id = col.decks.id_for_name(new_deck_name)
    new_model = col.models.by_name(new_model_name)
    new_names = [f["name"] for f in new_model["flds"]]
    with st.spinner("카드를 불러오는 중..."):
        rows = [cached_note_payload(col, n, media, deck_names) for n in ids]
    sig = hashlib.md5(
        ("|".join(str(n) for n in ids) + f"|{ver}|{new_model['id']}|{new_deck_id}").encode()
    ).hexdigest()[:12]
    grid_key = f"list-{sig}"
    st.session_state.grid_key = grid_key

    top = st.container()
    res = list_component(
        key=grid_key,
        data={
            "rows": rows, "offset": 0, "sig": sig,
            "new_names": new_names, "new_deck": htmllib.escape(new_deck_name),
        },
        on_star_change=on_star_event,
        on_changes_change=lambda: None,
    )
    changes = _plain(_get(res, "changes")) or {}
    n_edit = len(changes.get("edits") or {})
    n_add = sum(1 for a in (changes.get("adds") or []) if has_content(a.get("fields") or []))
    n_del = len(changes.get("dels") or [])
    with top:
        c1, c2 = st.columns([3, 2])
        confirm_del = True
        if n_del:
            confirm_del = c2.checkbox(f"노트 {n_del}개를 삭제합니다 (카드도 함께 삭제됨)")
        pending = n_edit or n_add or n_del
        if pending:
            c1.write(f"저장하지 않은 변경: 수정 {n_edit} · 추가 {n_add} · 삭제 {n_del}")
        if st.button("변경사항 저장", type="primary", disabled=not (pending and confirm_del)):
            ne, na, nd, errs, unplaced = apply_changes(col, changes, new_deck_id, new_model["id"])
            msg = f"저장했습니다: 수정 {ne} · 추가 {na} · 삭제 {nd}"
            sync_failed = False
            if st.session_state.get("auth") is not None and st.session_state.get("dirty"):
                with st.spinner("AnkiWeb과 동기화하는 중..."):
                    try:
                        ok, sync_msg = sync_back()
                    except Exception as e:
                        ok, sync_msg = False, f"동기화에 실패했습니다: {e}"
                if ok:
                    msg += "\n\nAnkiWeb과 동기화했습니다."
                else:
                    sync_failed = True
                    msg += f"\n\n{sync_msg}\n\n저장한 내용은 이 앱에 남아 있습니다. 사이드바의 \"AnkiWeb에 동기화\"로 다시 시도할 수 있어요."
            if unplaced:
                msg += (f"\n\n복습 중인 카드 아래에 추가한 {unplaced}장은 학습 순서 위치를 "
                        "지정할 수 없어 새 카드 맨 뒤에 들어갔습니다.")
            if errs:
                msg += "\n\n" + "\n".join(errs)
            st.session_state.flash = ("warn" if (errs or unplaced or sync_failed) else "ok", msg)
            st.rerun()
        st.caption("저장하면 AnkiWeb에도 바로 동기화됩니다. 필터나 정렬을 바꾸면 저장하지 않은 변경이 사라집니다.")
else:
    with st.spinner("카드를 불러오는 중..."):
        cards = [cached_card_payload(col, c, media, deck_names) for c in ids]
    sig = hashlib.md5(
        ("|".join(str(c) for c in ids) + f"|{both}|{ver}").encode()
    ).hexdigest()[:12]
    grid_key = f"grid-{sig}"
    st.session_state.grid_key = grid_key
    grid_component(
        key=grid_key,
        data={"cards": cards, "both": both},
        on_star_change=on_star_event,
    )
