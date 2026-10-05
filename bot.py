import asyncio
import html
import logging
import os
import re
import secrets
import shutil
import subprocess
import time

from aiohttp import ClientSession, web
from telethon import Button, TelegramClient, errors, events
from telethon.sessions import StringSession

from mega_api import Cancelled, MegaClient, MegaError
from store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.strip()}
if not ADMIN_IDS:
    raise SystemExit("ADMIN_IDS is required (your Telegram numeric user id)")
PORT = int(os.getenv("PORT", "10000"))
TMP = os.getenv("TMP_DIR", "/tmp/megabot")
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL_TASKS", "2"))
PAGE = 8
TG_LIMIT = 2000 * 1024 * 1024
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".3gp", ".mpg", ".mpeg"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
AUD_EXT = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"}
ARC_EXT = {".zip", ".rar", ".7z", ".tar", ".gz"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
os.makedirs(TMP, exist_ok=True)

store = Store()
tg = TelegramClient(StringSession(), API_ID, API_HASH)
tg.parse_mode = "html"
sem = asyncio.Semaphore(MAX_PARALLEL)


# ------------------------------------------------------------------ helpers
def esc(s):
    return html.escape(str(s))


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def fmt_time(s):
    s = int(max(s, 0))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


def trunc(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "file").strip() or "file"
    base, ext = os.path.splitext(name)
    return base[:100] + ext[:10]


def is_video(name):
    return os.path.splitext(name or "")[1].lower() in VIDEO_EXT


def icon(n):
    if n["t"] == 1:
        return "📁"
    ext = os.path.splitext(n["name"] or "")[1].lower()
    return ("🎬" if ext in VIDEO_EXT else "🖼" if ext in IMG_EXT else
            "🎵" if ext in AUD_EXT else "🗜" if ext in ARC_EXT else "📄")


def make_thumb(path):
    try:
        import imageio_ffmpeg
        out = path + ".thumb.jpg"
        for ss in ("00:00:01", "00:00:00"):
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-ss", ss, "-i", path, "-frames:v", "1",
                            "-vf", "scale=320:-2", out], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=90)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return out
    except Exception:
        pass
    return None


def tg_filename(m):
    f = m.file
    name = f.name if f and f.name else None
    ext = (f.ext if f else "") or ".mp4"
    if not name:
        name = f"video_{m.date:%Y%m%d_%H%M%S}_{m.id}{ext}"
    elif not os.path.splitext(name)[1]:
        name += ext
    return name


# ------------------------------------------------------------------ state
class Acc:
    def __init__(self, aid, owner, email, client):
        self.aid, self.owner, self.email, self.client = aid, owner, email, client


class Prog:
    def __init__(self):
        self.stage, self.name, self.done, self.total = "", "", 0, 0
        self.t0, self.idx, self.count = time.time(), 0, 0

    def set(self, stage, total):
        self.stage, self.done, self.total, self.t0 = stage, 0, total, time.time()

    def cb(self, done, total=None):
        self.done = done
        if total:
            self.total = total


class Task:
    def __init__(self, uid, title):
        self.id, self.uid, self.title = secrets.token_hex(3), uid, title
        self.cancel, self.p = False, Prog()

    def check(self):
        if self.cancel:
            raise Cancelled()


class Pend:
    def __init__(self):
        self.msgs, self.picker, self.timer, self.stage = [], None, None, "accounts"


accounts = {}       # aid -> Acc
nav = {}            # uid -> browsing state
await_input = {}    # uid -> text-input state
pending = {}        # uid -> Pend (files waiting for upload destination)
tasks = {}          # tid -> Task


def user_accs(uid):
    return [a for a in accounts.values() if a.owner == uid]


def get_acc(uid, aid):
    a = accounts.get(aid)
    if not a or a.owner != uid:
        raise MegaError(-11, "Account not found")
    return a


async def ensure_loaded(acc, force=False):
    c = acc.client
    if force or not c.nodes or time.time() - c.loaded_at > 900:
        await asyncio.to_thread(c.load)


# ------------------------------------------------------------------ views
def main_menu(uid):
    text = (f"🤖 <b>MEGA Manager</b>\n\nসংযুক্ত অ্যাকাউন্ট: <b>{len(user_accs(uid))}</b>\n\n"
            "💡 টেলিগ্রামের ভিডিও/ফাইল এখানে ফরওয়ার্ড করলেই MEGA-তে আপলোডের অপশন আসবে।")
    return text, [[Button.inline("📂 অ্যাকাউন্টসমূহ", "accs"), Button.inline("➕ অ্যাকাউন্ট যোগ", "add")],
                  [Button.inline("📋 চলমান কাজ", "tasks"), Button.inline("❓ সাহায্য", "help")]]


def accounts_view(uid):
    rows = [[Button.inline(f"📧 {a.email}", f"acc:{a.aid}")] for a in user_accs(uid)]
    rows.append([Button.inline("➕ অ্যাকাউন্ট যোগ", "add"), Button.inline("🏠 মেনু", "menu")])
    return "📂 <b>তোমার MEGA অ্যাকাউন্ট</b>" if len(rows) > 1 else "কোনো অ্যাকাউন্ট নেই।", rows


def picker_accounts(uid):
    p = pending[uid]
    rows = [[Button.inline(f"📧 {a.email}", f"pk:{a.aid}")] for a in user_accs(uid)]
    rows.append([Button.inline("❌ বাতিল", "cancel")])
    return f"📤 <b>{len(p.msgs)}টি ফাইল</b> পাওয়া গেছে।\nকোন MEGA অ্যাকাউন্টে আপলোড করবে?", rows


def folder_view(uid):
    st = nav[uid]
    acc = accounts[st["aid"]]
    c = acc.client
    aid, stack, pick, root = st["aid"], st["stack"], st["mode"] == "pick", st["root"]
    items = list(c.children(stack[-1])) if stack else c.shared_roots()
    if pick:
        items = [n for n in items if n["t"] == 1]
    items.sort(key=lambda n: (n["t"] != 1, (n["name"] or "").lower()))
    pages = max(1, -(-len(items) // PAGE))
    st["page"] = max(0, min(st["page"], pages - 1))
    page = st["page"]
    view = items[page * PAGE:(page + 1) * PAGE]
    names = [c.nodes[h]["name"] for h in stack if h in c.nodes]
    path = " / ".join(names) if names else "🤝 Shared Items"
    nfold = sum(1 for n in items if n["t"] == 1)
    sel = st["sel"]
    head = f"📧 {esc(acc.email)}\n📍 <b>{esc(path)}</b>\n📁 {nfold} ফোল্ডার · 📄 {len(items) - nfold} ফাইল"
    if pick:
        head = "📤 <b>আপলোডের ফোল্ডার বাছাই করো</b>\n" + head
    elif sel:
        head += f"\n✅ নির্বাচিত: {len(sel)}"
    if not items:
        head += "\n\n<i>(খালি)</i>"
    rows = []
    for n in view:
        nm = trunc(n["name"] or "?", 34)
        mark = "✅" if n["h"] in sel else "☐"
        if n["t"] == 1:
            row = [Button.inline(f"📁 {nm}", f"o:{aid}:{n['h']}")]
            if not pick:
                row.append(Button.inline(mark, f"t:{aid}:{n['h']}"))
        else:
            row = [Button.inline(f"{mark} {icon(n)} {nm} · {human(n['s'])}", f"t:{aid}:{n['h']}")]
        rows.append(row)
    if pages > 1:
        rows.append([Button.inline("◀️", f"pg:{page - 1}"), Button.inline(f"{page + 1}/{pages}", "noop"),
                     Button.inline("▶️", f"pg:{page + 1}")])
    if pick:
        cnt = len(pending[uid].msgs) if uid in pending else 0
        rows.append([Button.inline(f"📍 এখানে আপলোড ({cnt} ফাইল)", "go")])
        rows.append([Button.inline("⬆️ উপরে", "up"), Button.inline("❌ বাতিল", "cancel")])
    else:
        writable = root in ("d", "t")
        top = []
        if any(n["t"] == 0 for n in items):
            top.append(Button.inline("☑️ সব ফাইল", "sa"))
        if sel:
            top.append(Button.inline("✖️ ক্লিয়ার", "sc"))
        if top:
            rows.append(top)
        if sel:
            rows.append([Button.inline(f"📥 টেলিগ্রামে আনো ({len(sel)})", "dl")])
            ops = [Button.inline("🔗 লিংক", "ln")]
            if writable:
                ops += [Button.inline("✏️ রিনেম", "rn"),
                        Button.inline("🗑 স্থায়ী ডিলিট" if root == "t" else "🗑 ডিলিট", "del")]
            rows.append(ops)
        if root == "d":
            rows.append([Button.inline("📁 নতুন ফোল্ডার", "mk")])
        rows.append([Button.inline("⬆️ উপরে", "up"), Button.inline("🔄 রিফ্রেশ", "rf"),
                     Button.inline("🏠 অ্যাকাউন্ট", f"acc:{aid}")])
    return head, rows


async def show(ev, text, buttons=None):
    try:
        await ev.edit(text, buttons=buttons, link_preview=False)
    except errors.MessageNotModifiedError:
        pass


async def edit_msg(msg, text, buttons=None):
    try:
        await tg.edit_message(msg, text, buttons=buttons, link_preview=False)
    except errors.MessageNotModifiedError:
        pass


async def show_folder(ev, uid):
    t, b = folder_view(uid)
    await show(ev, t, b)


# ------------------------------------------------------------------ progress / tasks
def render(task):
    p = task.p
    if not p.stage:
        return f"{task.title}\n\n⏳ কিউতে অপেক্ষা করছে..."
    pct = p.done / p.total if p.total else 0
    el = max(time.time() - p.t0, 0.1)
    sp = p.done / el
    eta = (p.total - p.done) / sp if sp > 0 else 0
    bar = "█" * int(pct * 12) + "░" * (12 - int(pct * 12))
    return (f"{task.title}\n\n📄 <code>{esc(trunc(p.name, 60))}</code>  ({p.idx}/{p.count})\n"
            f"{p.stage}\n<code>[{bar}] {pct * 100:.1f}%</code>\n"
            f"{human(p.done)} / {human(p.total)}\n⚡ {human(sp)}/s · ⏱ {fmt_time(eta)}")


async def ticker(task, msg):
    last = ""
    btn = [[Button.inline("❌ বাতিল", f"cx:{task.id}")]]
    while True:
        text = render(task)
        if text != last:
            try:
                await edit_msg(msg, text, btn)
                last = text
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception as e:
                log.debug("ticker: %s", e)
        await asyncio.sleep(3)


async def run_task(task, msg, job, after):
    tasks[task.id] = task
    tick = asyncio.create_task(ticker(task, msg))
    try:
        async with sem:
            summary = await job(task)
    except Cancelled:
        summary = "🛑 বাতিল করা হয়েছে।"
    except Exception as e:
        log.exception("task failed")
        summary = f"❌ ব্যর্থ: {esc(e)}"
    finally:
        tick.cancel()
        tasks.pop(task.id, None)
    await edit_msg(msg, f"{task.title}\n\n{summary}", after)


def collect_files(c, handles):
    out, seen = [], set()
    for h in handles:
        n = c.nodes.get(h)
        if not n:
            continue
        for f in ([n] if n["t"] == 0 else c.walk_files(h)):
            if f["h"] not in seen:
                seen.add(f["h"])
                out.append(f)
    return out


async def job_download(task, acc, files, chat):
    p, ok, fails = task.p, 0, []
    p.count = len(files)
    for i, n in enumerate(files, 1):
        task.check()
        p.idx, p.name = i, n["name"]
        if n["s"] > TG_LIMIT:
            fails.append(f"{n['name']} (২GB-র বেশি)")
            continue
        d = os.path.join(TMP, f"{task.id}_{i}")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, safe_name(n["name"]))

        def tcb(done, total):
            task.check()
            p.cb(done, total)

        try:
            p.set("⬇️ MEGA → সার্ভার", n["s"])
            await asyncio.to_thread(acc.client.download, n, path, p.cb, task.check)
            p.set("⬆️ সার্ভার → টেলিগ্রাম", os.path.getsize(path))
            vid = is_video(n["name"])
            thumb = await asyncio.to_thread(make_thumb, path) if vid else None
            await tg.send_file(task.chat, path, caption=esc(n["name"]), force_document=not vid,
                               supports_streaming=vid, thumb=thumb, progress_callback=tcb)
            ok += 1
        except Cancelled:
            raise
        except Exception as e:
            fails.append(f"{n['name']}: {e}")
        finally:
            shutil.rmtree(d, ignore_errors=True)
    res = f"✅ সম্পন্ন: {ok}/{len(files)}"
    if fails:
        res += "\n\n❌ ব্যর্থ:\n" + "\n".join(f"• {esc(trunc(x, 80))}" for x in fails[:10])
    return res


async def job_upload(task, acc, dest, msgs):
    p, ok, fails = task.p, 0, []
    p.count = len(msgs)
    for i, m in enumerate(msgs, 1):
        task.check()
        name = tg_filename(m)
        p.idx, p.name = i, name
        d = os.path.join(TMP, f"{task.id}_{i}")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, safe_name(name))

        def tcb(done, total):
            task.check()
            p.cb(done, total)

        try:
            p.set("⬇️ টেলিগ্রাম → সার্ভার", m.file.size or 0)
            await tg.download_media(m, file=path, progress_callback=tcb)
            p.set("⬆️ সার্ভার → MEGA", os.path.getsize(path))
            await asyncio.to_thread(acc.client.upload, path, dest, name, p.cb, task.check)
            ok += 1
        except Cancelled:
            raise
        except Exception as e:
            fails.append(f"{name}: {e}")
        finally:
            shutil.rmtree(d, ignore_errors=True)
    res = f"✅ আপলোড সম্পন্ন: {ok}/{len(msgs)}\n📧 {esc(acc.email)}"
    if fails:
        res += "\n\n❌ ব্যর্থ:\n" + "\n".join(f"• {esc(trunc(x, 80))}" for x in fails[:10])
    return res


def after_buttons(uid):
    rows = []
    if uid in nav and nav[uid]["mode"] == "browse":
        rows.append([Button.inline("⬅️ ফোল্ডারে ফিরো", "back")])
    rows.append([Button.inline("🏠 মেনু", "menu")])
    return rows


# ------------------------------------------------------------------ callbacks
@tg.on(events.CallbackQuery)
async def on_callback(ev):
    uid = ev.sender_id
    if uid not in ADMIN_IDS:
        return await ev.answer("⛔ অনুমতি নেই", alert=True)
    parts = ev.data.decode().split(":")
    cmd, a = parts[0], parts[1:]
    try:
        if cmd != "cx":
            await ev.answer()
        await dispatch(ev, uid, cmd, a)
    except errors.MessageNotModifiedError:
        pass
    except MegaError as e:
        await show(ev, f"❌ MEGA: {esc(e)}", [[Button.inline("🏠 মেনু", "menu")]])
    except Exception as e:
        log.exception("callback error")
        await show(ev, f"❌ Error: {esc(e)}", [[Button.inline("🏠 মেনু", "menu")]])


def clear_flow(uid):
    await_input.pop(uid, None)
    p = pending.pop(uid, None)
    if p and p.timer:
        p.timer.cancel()


async def dispatch(ev, uid, cmd, a):
    if cmd == "noop":
        return
    if cmd == "menu":
        clear_flow(uid)
        nav.pop(uid, None)
        return await show(ev, *main_menu(uid))
    if cmd == "cancel":
        clear_flow(uid)
        return await show(ev, *main_menu(uid))
    if cmd == "help":
        return await show(ev, (
            "❓ <b>সাহায্য</b>\n\n• <b>অ্যাকাউন্ট যোগ</b>: ইমেইল → পাসওয়ার্ড → (2FA থাকলে) কোড\n"
            "• <b>আপলোড</b>: ভিডিও/ফাইল এখানে পাঠাও বা ফরওয়ার্ড করো → অ্যাকাউন্ট → ফোল্ডার → আপলোড\n"
            "• <b>ডাউনলোড</b>: অ্যাকাউন্ট → ফোল্ডার → ফাইল/ফোল্ডার সিলেক্ট → 📥\n"
            "• টেলিগ্রামের সীমা: সর্বোচ্চ ২GB/ফাইল"), [[Button.inline("🏠 মেনু", "menu")]])
    if cmd == "accs":
        return await show(ev, *accounts_view(uid))
    if cmd == "add":
        await_input[uid] = {"kind": "email", "msg": await ev.get_message()}
        return await show(ev, "📧 তোমার <b>MEGA ইমেইল</b> পাঠাও:", [[Button.inline("❌ বাতিল", "cancel")]])
    if cmd == "tasks":
        mine = [t for t in tasks.values() if t.uid == uid]
        rows = [[Button.inline(f"❌ {trunc(re.sub('<[^>]+>', '', t.title), 28)} ({t.id})", f"cx:{t.id}")] for t in mine]
        rows.append([Button.inline("🏠 মেনু", "menu")])
        return await show(ev, f"📋 চলমান কাজ: <b>{len(mine)}</b>\n(বাতিল করতে বাটন চাপো)", rows)
    if cmd == "cx":
        t = tasks.get(a[0])
        if t and t.uid == uid:
            t.cancel = True
            return await ev.answer("বাতিল করা হচ্ছে...")
        return await ev.answer("কাজটি আর চলছে না")

    if cmd == "acc":
        acc = get_acc(uid, a[0])
        txt = f"📧 <b>{esc(acc.email)}</b>"
        try:
            used, tot = await asyncio.to_thread(acc.client.quota)
            txt += f"\n💾 {human(used)} / {human(tot)}"
        except Exception:
            pass
        return await show(ev, txt, [
            [Button.inline("☁️ Cloud Drive", f"br:{acc.aid}:d")],
            [Button.inline("🤝 Shared Items", f"br:{acc.aid}:s"), Button.inline("🗑 Rubbish", f"br:{acc.aid}:t")],
            [Button.inline("🚪 রিমুভ", f"rm:{acc.aid}"), Button.inline("⬅️ পেছনে", "accs")]])
    if cmd == "rm":
        acc = get_acc(uid, a[0])
        return await show(ev, f"⚠️ <b>{esc(acc.email)}</b> বট থেকে সরাবে? (MEGA অ্যাকাউন্ট ডিলিট হবে না)",
                          [[Button.inline("✅ হ্যাঁ", f"rmok:{acc.aid}"), Button.inline("❌ না", f"acc:{acc.aid}")]])
    if cmd == "rmok":
        acc = get_acc(uid, a[0])
        await asyncio.to_thread(store.remove, acc.aid)
        accounts.pop(acc.aid, None)
        return await show(ev, *accounts_view(uid))

    if cmd in ("br", "pk"):
        acc = get_acc(uid, a[0])
        root = a[1] if cmd == "br" else "d"
        await show(ev, "⏳ লোড হচ্ছে...")
        await ensure_loaded(acc)
        c = acc.client
        stack = [] if root == "s" else [c.root(2 if root == "d" else 4)]
        if root != "s" and stack[0] is None:
            raise MegaError(-9, "Root folder not found")
        nav[uid] = {"aid": acc.aid, "root": root, "stack": stack, "page": 0, "sel": set(),
                    "mode": "pick" if cmd == "pk" else "browse"}
        if cmd == "pk" and uid in pending:
            pending[uid].stage = "folder"
        return await show_folder(ev, uid)

    # --- everything below needs nav state
    st = nav.get(uid)
    if cmd in ("o", "t") and st:
        st["aid"] = a[0]
    if not st:
        return await show(ev, "⚠️ সেশন শেষ। আবার শুরু করো।", [[Button.inline("🏠 মেনু", "menu")]])
    acc = get_acc(uid, st["aid"])
    c = acc.client

    if cmd == "back":
        return await show_folder(ev, uid)
    if cmd == "o":
        n = c.nodes.get(a[1])
        if n and n["t"] in (1, 2, 4):
            st["stack"].append(a[1])
            st["page"] = 0
        return await show_folder(ev, uid)
    if cmd == "up":
        min_len = 0 if st["root"] == "s" else 1
        if len(st["stack"]) > min_len:
            st["stack"].pop()
            st["page"] = 0
            return await show_folder(ev, uid)
        if st["mode"] == "pick" and uid in pending:
            pending[uid].stage = "accounts"
            return await show(ev, *picker_accounts(uid))
        return await show(ev, "📧 " + esc(acc.email), [
            [Button.inline("☁️ Cloud Drive", f"br:{acc.aid}:d")],
            [Button.inline("🤝 Shared Items", f"br:{acc.aid}:s"), Button.inline("🗑 Rubbish", f"br:{acc.aid}:t")],
            [Button.inline("⬅️ পেছনে", "accs")]])
    if cmd == "pg":
        st["page"] = int(a[0])
        return await show_folder(ev, uid)
    if cmd == "t":
        st["sel"] ^= {a[1]}
        return await show_folder(ev, uid)
    if cmd == "sa":
        cur = c.children(st["stack"][-1]) if st["stack"] else c.shared_roots()
        st["sel"] |= {n["h"] for n in cur if n["t"] == 0}
        return await show_folder(ev, uid)
    if cmd == "sc":
        st["sel"].clear()
        return await show_folder(ev, uid)
    if cmd == "rf":
        await show(ev, "⏳ রিফ্রেশ হচ্ছে...")
        await ensure_loaded(acc, force=True)
        st["stack"] = [h for h in st["stack"] if h in c.nodes]
        st["sel"] = {h for h in st["sel"] if h in c.nodes}
        return await show_folder(ev, uid)

    if cmd == "dl":
        files = collect_files(c, st["sel"])
        if not files:
            return await show(ev, "⚠️ কোনো ফাইল পাওয়া যায়নি।", [[Button.inline("⬅️ ফিরো", "back")]])
        total = sum(f["s"] for f in files)
        task = Task(uid, f"📥 <b>MEGA → টেলিগ্রাম</b> ({len(files)} ফাইল, {human(total)})")
        task.chat = ev.chat_id
        msg = await ev.get_message()
        st["sel"].clear()
        asyncio.create_task(run_task(task, msg, lambda t: job_download(t, acc, files, ev.chat_id), after_buttons(uid)))
        return
    if cmd == "go":
        p = pending.pop(uid, None)
        if not p or not st["stack"]:
            return await show(ev, "⚠️ আপলোডের জন্য কোনো ফাইল নেই।", [[Button.inline("🏠 মেনু", "menu")]])
        dest = st["stack"][-1]
        folder = c.nodes[dest]["name"]
        task = Task(uid, f"📤 <b>টেলিগ্রাম → MEGA</b> ({len(p.msgs)} ফাইল)\n📁 {esc(folder)}")
        task.chat = ev.chat_id
        msg = await ev.get_message()
        asyncio.create_task(run_task(task, msg, lambda t: job_upload(t, acc, dest, p.msgs), after_buttons(uid)))
        return
    if cmd == "ln":
        files = collect_files(c, st["sel"])
        lines = []
        for f in files[:20]:
            try:
                link = await asyncio.to_thread(c.export_link, f)
                lines.append(f"🔗 {esc(f['name'])}\n<code>{link}</code>")
            except MegaError as e:
                lines.append(f"❌ {esc(f['name'])}: {esc(e)}")
        await ev.respond("\n\n".join(lines) or "শুধু ফাইলের লিংক তৈরি করা যায়।")
        return
    if cmd == "mk":
        await_input[uid] = {"kind": "mkdir", "msg": await ev.get_message()}
        return await show(ev, "📁 নতুন ফোল্ডারের <b>নাম</b> পাঠাও:", [[Button.inline("❌ বাতিল", "back")]])
    if cmd == "rn":
        if len(st["sel"]) != 1:
            return await ev.respond("⚠️ ঠিক ১টি আইটেম সিলেক্ট করো।")
        await_input[uid] = {"kind": "rename", "msg": await ev.get_message(), "h": next(iter(st["sel"]))}
        return await show(ev, "✏️ নতুন <b>নাম</b> পাঠাও:", [[Button.inline("❌ বাতিল", "back")]])
    if cmd == "del":
        perm = st["root"] == "t"
        txt = f"⚠️ {len(st['sel'])}টি আইটেম " + ("<b>স্থায়ীভাবে মুছবে?</b>" if perm else "Rubbish Bin-এ পাঠাবে?")
        return await show(ev, txt, [[Button.inline("✅ হ্যাঁ", "delok"), Button.inline("❌ না", "back")]])
    if cmd == "delok":
        perm, done = st["root"] == "t", 0
        for h in list(st["sel"]):
            n = c.nodes.get(h)
            if n and n["u"] == c.uid:
                await asyncio.to_thread(c.delete_perm if perm else c.trash, h)
                done += 1
        st["sel"].clear()
        await show_folder(ev, uid)
        return await ev.respond(f"🗑 {done}টি আইটেম সম্পন্ন।")


# ------------------------------------------------------------------ messages
@tg.on(events.NewMessage(func=lambda e: e.is_private))
async def on_message(ev):
    uid = ev.sender_id
    if uid not in ADMIN_IDS:
        return await ev.respond("⛔ এই বটটি প্রাইভেট।")
    m = ev.message
    text = (m.raw_text or "").strip()
    if text.startswith("/"):
        if text.split()[0].split("@")[0] in ("/start", "/menu", "/cancel"):
            clear_flow(uid)
            nav.pop(uid, None)
            t, b = main_menu(uid)
            return await ev.respond(t, buttons=b)
        return
    if (m.video or m.document) and not (m.sticker or m.voice):
        return await on_media(ev, uid)
    st = await_input.get(uid)
    if st and text:
        await handle_input(ev, uid, st, text)


async def on_media(ev, uid):
    if not user_accs(uid):
        return await ev.respond("⚠️ আগে একটি MEGA অ্যাকাউন্ট যোগ করো।", buttons=[[Button.inline("➕ অ্যাকাউন্ট যোগ", "add")]])
    p = pending.setdefault(uid, Pend())
    p.msgs.append(ev.message)
    if p.timer:
        p.timer.cancel()
    p.timer = asyncio.create_task(show_picker(uid, ev.chat_id, p))


async def show_picker(uid, chat, p):
    await asyncio.sleep(1.5)
    try:
        t, b = picker_accounts(uid)
        if p.picker is None:
            p.picker = await tg.send_message(chat, t, buttons=b)
        elif p.stage == "accounts":
            await edit_msg(p.picker, t, b)
    except Exception as e:
        log.warning("picker: %s", e)


async def handle_input(ev, uid, st, text):
    kind, msg = st["kind"], st["msg"]
    try:
        await ev.delete()  # keep chat clean / hide password
    except Exception:
        pass
    cancel = [[Button.inline("❌ বাতিল", "cancel")]]
    if kind == "email":
        if not EMAIL_RE.match(text):
            return await edit_msg(msg, "❌ ইমেইল ঠিক নয়। আবার পাঠাও:", cancel)
        st.update(kind="password", email=text.lower())
        return await edit_msg(msg, f"📧 {esc(st['email'])}\n\n🔑 এবার <b>পাসওয়ার্ড</b> পাঠাও\n"
                                   "<i>(পাঠানোর সাথে সাথে মেসেজ মুছে যাবে)</i>", cancel)
    if kind in ("password", "mfa"):
        mfa = None
        if kind == "password":
            st["password"] = text
        else:
            mfa = re.sub(r"\D", "", text)
        await edit_msg(msg, "⏳ লগইন হচ্ছে...")
        c = MegaClient()
        try:
            await asyncio.to_thread(c.login, st["email"], st["password"], mfa)
        except MegaError as e:
            if e.code == -26:
                if kind == "password":
                    st["kind"] = "mfa"
                    return await edit_msg(msg, "🔢 এই অ্যাকাউন্টে 2FA চালু আছে।\n<b>Authenticator-এর ৬ ডিজিট কোড</b> পাঠাও:", cancel)
                return await edit_msg(msg, "❌ কোড ভুল বা মেয়াদ শেষ। নতুন কোড পাঠাও:", cancel)
            await_input.pop(uid, None)
            return await edit_msg(msg, f"❌ লগইন ব্যর্থ: {esc(e)}",
                                  [[Button.inline("🔁 আবার চেষ্টা", "add"), Button.inline("🏠 মেনু", "menu")]])
        except Exception as e:
            await_input.pop(uid, None)
            return await edit_msg(msg, f"❌ Error: {esc(e)}", [[Button.inline("🏠 মেনু", "menu")]])
        await_input.pop(uid, None)
        old = next((x for x in user_accs(uid) if x.email == st["email"]), None)
        aid = old.aid if old else secrets.token_hex(3)
        await asyncio.to_thread(store.add, aid, uid, st["email"], c.export_session())
        accounts[aid] = Acc(aid, uid, st["email"], c)
        return await edit_msg(msg, f"✅ <b>{esc(st['email'])}</b> যুক্ত হয়েছে!",
                              [[Button.inline("📂 অ্যাকাউন্টসমূহ", "accs"), Button.inline("🏠 মেনু", "menu")]])
    nv = nav.get(uid)
    if kind in ("mkdir", "rename") and nv:
        acc = accounts[nv["aid"]]
        await_input.pop(uid, None)
        try:
            if kind == "mkdir":
                await asyncio.to_thread(acc.client.mkdir, nv["stack"][-1], text)
            else:
                await asyncio.to_thread(acc.client.rename, st["h"], text)
                nv["sel"].clear()
        except Exception as e:
            await ev.respond(f"❌ {esc(e)}")
        t, b = folder_view(uid)
        await edit_msg(msg, t, b)


# ------------------------------------------------------------------ web server / startup
async def start_web():
    app = web.Application()
    ok = lambda r: web.Response(text="OK")
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()


async def self_ping():
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    async with ClientSession() as s:
        while True:
            await asyncio.sleep(600)
            try:
                await s.get(url + "/health", timeout=20)
            except Exception:
                pass


async def main():
    await start_web()
    for r in await asyncio.to_thread(store.all):
        accounts[r["aid"]] = Acc(r["aid"], r["owner"], r["email"], MegaClient.from_session(r["session"]))
    log.info("Loaded %d account(s)", len(accounts))
    await tg.start(bot_token=BOT_TOKEN)
    asyncio.create_task(self_ping())
    log.info("Bot started")
    await tg.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
