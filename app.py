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
# 컴포넌트: 카드 격자 / 서식 편집기
# ----------------------------------------------------------------------------

GRID_HTML = """
<div class="bar">
  <button class="all-front">모두 앞면</button>
  <button class="all-back">모두 뒷면</button>
</div>
<div class="grid"></div>
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
"""

GRID_JS = """
export default function(component) {
  const { data, setTriggerValue, parentElement } = component;
  const root = parentElement.querySelector('.grid');
  const flipped = parentElement.__flipped || (parentElement.__flipped = new Set());
  root.innerHTML = '';
  if (!data.cards.length) {
    root.innerHTML = '<div class="empty">조건에 맞는 카드가 없습니다.</div>';
  }
  const els = [];
  data.cards.forEach((c) => {
    const el = document.createElement('div');
    el.className = 'card s-' + c.state_key + (data.both ? ' both' : '');
    if (!data.both && flipped.has(c.id)) el.classList.add('flipped');
    el.innerHTML =
      '<div class="meta"><span>' + c.state + '</span><span class="deck">' + c.deck + '</span>' +
      '<span class="acts"><button class="star" title="별표">' + (c.marked ? '★' : '☆') + '</button>' +
      '<button class="edit">수정</button></span></div>' +
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
      if (b && b.classList.contains('edit')) {
        setTriggerValue('edit', { nid: c.nid });
        return;
      }
      if (data.both) return;
      el.classList.toggle('flipped');
      if (el.classList.contains('flipped')) flipped.add(c.id); else flipped.delete(c.id);
    });
    root.appendChild(el);
    els.push([c, el]);
  });
  const setAll = (on) => {
    els.forEach(([c, el]) => {
      el.classList.toggle('flipped', on);
      if (on) flipped.add(c.id); else flipped.delete(c.id);
    });
  };
  parentElement.querySelector('.all-front').onclick = () => setAll(false);
  parentElement.querySelector('.all-back').onclick = () => setAll(true);
}
"""

EDITOR_HTML = """
<div class="wrap">
  <div class="tools">
    <button data-cmd="bold" title="굵게 (Ctrl+B)"><b>B</b></button>
    <button data-cmd="italic" title="기울임 (Ctrl+I)"><i>I</i></button>
    <button data-cmd="underline" title="밑줄 (Ctrl+U)"><u>U</u></button>
    <button data-cmd="strikeThrough" title="취소선"><s>S</s></button>
    <span class="sep"></span>
    <button data-cmd="hiliteColor" data-val="#ffe94d" title="형광펜">형광펜</button>
    <button data-cmd="hiliteColor" data-val="transparent" title="형광펜 해제">형광펜 해제</button>
    <label class="color" title="글자색">글자색 <input type="color" value="#d03030"></label>
    <span class="sep"></span>
    <button data-cmd="superscript" title="위 첨자">x²</button>
    <button data-cmd="subscript" title="아래 첨자">x₂</button>
    <button data-cmd="insertUnorderedList" title="글머리 기호">목록</button>
    <button data-cmd="removeFormat" title="서식 지우기">서식 지우기</button>
  </div>
  <div class="ed" contenteditable="true"></div>
</div>
"""

EDITOR_CSS = """
.wrap { border: 1px solid var(--st-border-color, #c8c8c8); border-radius: 3px; }
.tools { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; padding: 4px 6px;
  border-bottom: 1px solid var(--st-border-color, #c8c8c8); }
.tools button, .tools .color {
  font: inherit; font-size: 13px; padding: 2px 8px; cursor: pointer; background: transparent; color: inherit;
  border: 1px solid var(--st-border-color, #c8c8c8); border-radius: 3px;
}
.tools .color input { vertical-align: middle; width: 22px; height: 18px; padding: 0; border: none; background: none; }
.sep { width: 1px; height: 18px; background: var(--st-border-color, #c8c8c8); margin: 0 4px; }
.ed { min-height: 80px; padding: 8px 10px; outline: none; font-size: 15px; line-height: 1.5; }
.ed img { max-width: 100%; height: auto; }
.ed .cloze { font-weight: bold; color: #2f6fd0; }
"""

EDITOR_JS = """
export default function(component) {
  const { data, setStateValue, parentElement } = component;
  const ed = parentElement.querySelector('.ed');
  if (ed.dataset.src !== data.html) {
    ed.innerHTML = data.html;
    ed.dataset.src = data.html;
  }
  const clean = () => {
    const c = ed.cloneNode(true);
    c.querySelectorAll('img[data-orig]').forEach((i) => {
      i.setAttribute('src', i.dataset.orig);
      i.removeAttribute('data-orig');
    });
    return c.innerHTML
      .replace(/<div><br\\s*\\/?><\\/div>/gi, '<br>')
      .replace(/<div>/gi, '<br>')
      .replace(/<\\/div>/gi, '')
      .replace(/&nbsp;/g, ' ');
  };
  let timer = null;
  const emit = () => setStateValue('html', clean());
  ed.oninput = () => { clearTimeout(timer); timer = setTimeout(emit, 300); };
  ed.onblur = () => { clearTimeout(timer); if (ed.dataset.touched) emit(); };
  ed.addEventListener('input', () => { ed.dataset.touched = '1'; });
  const run = (cmd, val) => {
    ed.focus();
    document.execCommand('styleWithCSS', false, cmd === 'hiliteColor' || cmd === 'foreColor');
    document.execCommand(cmd, false, val || null);
    ed.dataset.touched = '1';
    clearTimeout(timer);
    timer = setTimeout(emit, 100);
  };
  parentElement.querySelectorAll('.tools button').forEach((b) => {
    b.onmousedown = (e) => e.preventDefault();
    b.onclick = () => run(b.dataset.cmd, b.dataset.val);
  });
  const col = parentElement.querySelector('.tools input[type=color]');
  col.onchange = () => run('foreColor', col.value);
}
"""

grid_component = st.components.v2.component(
    "anki_card_grid", html=GRID_HTML, css=GRID_CSS, js=GRID_JS
)
editor_component = st.components.v2.component(
    "anki_rich_editor", html=EDITOR_HTML, css=EDITOR_CSS, js=EDITOR_JS
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
    for k in ("auth", "dirty", "media_cache", "ver", "edit_nid", "grid_key", "seed"):
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
    st.session_state.ver = st.session_state.get("ver", 0) + 1
    st.session_state.media_cache = {}
    msg = out.server_message or "동기화 완료"
    return True, msg


def make_backup():
    col = st.session_state.col
    path = os.path.join(st.session_state.workdir, "backup.colpkg")
    col.export_collection_package(path, include_media=False, legacy=True)
    with open(path, "rb") as f:
        return f.read()


# ----------------------------------------------------------------------------
# 카드 읽기 / 편집
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


def card_payload(col, cid, media, deck_names):
    card = col.get_card(cid)
    note = card.note()
    q = card.question()
    a = card.answer()
    parts = ANSWER_SPLIT.split(a, maxsplit=1)
    back = parts[1] if len(parts) == 2 else a
    label, key = card_state(card)
    if card.did not in deck_names:
        deck_names[card.did] = col.decks.name(card.did)
    return {
        "id": int(cid),
        "nid": int(note.id),
        "front": sanitize(AV_RE.sub("🔊", q), media),
        "back": sanitize(AV_RE.sub("🔊", back), media),
        "state": label,
        "state_key": key,
        "deck": htmllib.escape(deck_names[card.did]),
        "marked": "marked" in [t.lower() for t in note.tags],
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


def on_grid_event():
    res = st.session_state.get(st.session_state.get("grid_key", ""))
    star = _get(res, "star")
    if star:
        toggle_star(int(star["nid"]), bool(star["on"]))
    edit = _get(res, "edit")
    if edit:
        st.session_state.edit_nid = int(edit["nid"])


@st.dialog("카드 수정", width="large")
def edit_dialog(nid):
    col = st.session_state.col
    media = media_resolver(col)
    note = col.get_note(nid)
    names = note.keys()
    st.caption("수정한 필드만 저장됩니다. 서식은 위 버튼으로 적용하세요.")
    originals, values = {}, {}
    for i, name in enumerate(names):
        st.markdown(f"**{name}**")
        orig = sanitize(note[name], media, edit=True)
        originals[name] = orig
        res = editor_component(
            key=f"ed-{nid}-{i}-{st.session_state.get('ver', 0)}",
            data={"html": orig},
            on_html_change=lambda: None,
        )
        values[name] = _get(res, "html")
    c1, c2 = st.columns(2)
    if c1.button("저장", type="primary", use_container_width=True):
        changed = 0
        for name in names:
            v = values[name]
            if v is not None and v != originals[name]:
                note[name] = v
                changed += 1
        if changed:
            col.update_note(note)
            st.session_state.dirty.add(nid)
            st.session_state.ver = st.session_state.get("ver", 0) + 1
        st.rerun()
    if c2.button("취소", use_container_width=True):
        st.rerun()


# ----------------------------------------------------------------------------
# 화면
# ----------------------------------------------------------------------------

st.title("Anki 카드 보기")

col = st.session_state.get("col")

if col is None:
    st.write("AnkiWeb 계정으로 로그인하면 덱을 내려받아 여러 카드를 한 화면에서 볼 수 있습니다.")
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

# ---- 사이드바: 필터 / 동기화
with st.sidebar:
    st.subheader("보기")
    deck_list = sorted(d.name for d in col.decks.all_names_and_ids())
    sel_decks = st.multiselect("덱", deck_list)
    sel_states = st.multiselect("카드 상태", list(STATE_QUERY.keys()))
    text = st.text_input("검색 (Anki 검색 문법 가능)")
    page_size = st.selectbox("한 페이지 카드 수", [12, 24, 48, 96], index=1)
    both = st.checkbox("앞면과 뒷면을 함께 보기")
    shuffle = st.checkbox("섞어서 보기")
    if shuffle and st.button("다시 섞기"):
        st.session_state.seed = random.random()

    st.divider()
    st.subheader("AnkiWeb 동기화")
    dirty = st.session_state.get("dirty", set())
    st.write(f"이 세션에서 수정한 노트: {len(dirty)}개")
    if st.button("백업 파일 만들기"):
        with st.spinner("만드는 중..."):
            st.session_state.backup = make_backup()
    if st.session_state.get("backup"):
        st.download_button("백업 내려받기 (.colpkg)", st.session_state.backup,
                           file_name="anki_backup.colpkg")
    confirm = st.checkbox(f"수정한 {len(dirty)}개 노트를 AnkiWeb에 반영합니다", disabled=not dirty)
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
try:
    cids = list(col.find_cards(query)) if query else list(col.find_cards(""))
except Exception as e:
    st.error(f"검색어를 해석하지 못했습니다: {e}")
    st.stop()

if shuffle:
    rnd = random.Random(st.session_state.get("seed", 0))
    rnd.shuffle(cids)

counts = {s: len(col.find_cards(q)) for s, q in STATE_QUERY.items()}
st.caption(
    f"전체 {len(col.find_cards(''))}장 · " + " · ".join(f"{s} {n}" for s, n in counts.items())
    + f" · 검색 결과 {len(cids)}장"
)

pages = max(1, -(-len(cids) // page_size))
page = st.number_input("페이지", min_value=1, max_value=pages, value=1, step=1)
chunk = cids[(page - 1) * page_size: page * page_size]

deck_names = {}
cards = [card_payload(col, c, media, deck_names) for c in chunk]

sig = hashlib.md5(
    ("|".join(str(c) for c in chunk) + f"|{both}|{st.session_state.get('ver', 0)}").encode()
).hexdigest()[:12]
grid_key = f"grid-{sig}"
st.session_state.grid_key = grid_key

nid = st.session_state.pop("edit_nid", None)

grid_component(
    key=grid_key,
    data={"cards": cards, "both": both},
    on_star_change=on_grid_event,
    on_edit_change=on_grid_event,
)

if nid:
    edit_dialog(nid)
