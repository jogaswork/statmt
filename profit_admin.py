"""Published profits: stable channel/message identities and replayable accounting."""
from copy import deepcopy
import re

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from telegram.error import BadRequest

STATUSES = {"lock": "🔒 Лок", "paid": "✅ Выплачено", "waiting": "⏳ Ожидание"}
BUTTONS_VERSION = 2
FIELDS = {"worker": "Воркер", "service": "Сервис", "tp": "ТП", "curator": "Куратор",
          "amount": "Сумма", "share": "Доля воркера"}
PAGE_SIZE = 8


def identity(post):
    return f"{post['channel']}:{post['message']}"


def status_keyboard(post):
    selected = post.get("status", "waiting")
    if selected not in STATUSES:
        selected = "waiting"
    return K([[B(STATUSES[selected], callback_data="pf_status_" + selected)]])


def find(data, key):
    return next((post for post in data["history"] if identity(post) == key), None)


def worker_key(top, name):
    normalized = name.lstrip("@#").casefold()
    return next((key for key in top if key.lstrip("@#").casefold() == normalized),
                "@" + name.lstrip("@#"))


def delta_top(top, old, new):
    """Keep all historic/manual totals; replace only this post's contribution."""
    result = dict(top)
    for values, sign in ((old, -1), (new, 1)):
        if values:
            key = worker_key(result, values["worker"])
            result[key] = int(result.get(key, 0)) + sign * int(values["share"])
    return result


def persist_change(data, post, values=None):
    from work import load_data, commit_profit_data
    old = deepcopy(post["values"])
    new = values if values is not None else old
    top = load_data()
    if old != new:
        top = delta_top(top, old, new)
    post["values"] = deepcopy(new)
    post["revision"] = post.get("revision", 0) + 1
    post["pending_sync"] = True
    commit_profit_data(data, top)


async def sync_post(bot, post, *, buttons_only=False):
    from profits import render
    kwargs = dict(chat_id=post["channel"], message_id=post["message"],
                  reply_markup=status_keyboard(post))
    try:
        if buttons_only or not post.get("template"):
            await bot.edit_message_reply_markup(**kwargs)
        else:
            body = render({"body": post["template"], "photo": post.get("photo")}, post["values"])
            if post.get("photo"):
                await bot.edit_message_caption(caption=body, parse_mode="HTML", **kwargs)
            else:
                await bot.edit_message_text(text=body, parse_mode="HTML", **kwargs)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def sync_saved(bot, key):
    from profits import get, save
    data = get()
    post = find(data, key)
    try:
        await sync_post(bot, post)
    except Exception:
        # Do not return raw Telegram exceptions: request URLs can contain credentials.
        return "⚠️ Сохранено, но канал не обновлён. Проверьте пост и право редактирования, затем нажмите «Повторить обновление»."
    post["pending_sync"] = False
    post["buttons_version"] = BUTTONS_VERSION
    save(data)
    return "✅ Сохранено, пост обновлён."


async def sync_history(application):
    """Attach indicators to tracked legacy posts, and retry durable pending edits."""
    import asyncio
    from profits import get, save
    keys = [identity(post) for post in get()["history"]
            if post.get("buttons_version") != BUTTONS_VERSION or post.get("pending_sync")]
    for key in keys:
        data = get(); post = find(data, key)
        try:
            await sync_post(application.bot, post, buttons_only=not post.get("pending_sync"))
        except Exception:
            post["pending_sync"] = True
        else:
            post["buttons_version"] = BUTTONS_VERSION
            post["pending_sync"] = False
        save(data)
        await asyncio.sleep(0.1)


async def show_list(message, context, page=0):
    from profits import get, keyboard, state
    query = context.user_data.get("profit_search", "")
    posts = [post for post in reversed(get()["history"])
             if query.casefold() in (identity(post) + " " + " ".join(str(v) for v in post["values"].values())).casefold()]
    page = max(0, min(page, max(0, (len(posts)-1)//PAGE_SIZE)))
    rows = []
    for post in posts[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
        v = post["values"]
        rows.append((f"#{post['message']} · {v.get('worker', '—')} · {v.get('amount', '—')} ₽"[:100],
                     "pf_manage_open_" + identity(post)))
    if page: rows.append(("⬅️ Предыдущие", f"pf_manage_page_{page-1}"))
    if (page+1)*PAGE_SIZE < len(posts): rows.append(("➡️ Следующие", f"pf_manage_page_{page+1}"))
    rows += [("🔎 Поиск / ссылка / пересланный пост", "pf_manage_search"),
             ("📋 Все профиты", "pf_manage_all"), ("⬅️ Назад", "pf_home")]
    state(context, mode="manage_list")
    await message.reply_text(f"📚 Опубликованные профиты · {len(posts)} · страница {page+1}", reply_markup=keyboard(rows))


async def show_post(message, context, key, notice=""):
    from profits import get, keyboard, state
    post = find(get(), key)
    if not post:
        await message.reply_text("Профит не найден. Откройте список заново.")
        return
    state(context, mode="manage_post", key=key, revision=post.get("revision", 0))
    rows = [("✏️ " + name, "pf_manage_field_" + field) for field, name in FIELDS.items()]
    rows += [("📝 Текст / шаблон поста", "pf_manage_body")]
    rows += [(label, "pf_manage_status_" + status) for status, label in STATUSES.items()]
    rows += [("🔄 Повторить обновление / добавить кнопки", "pf_manage_sync"),
             ("⬅️ К списку", "pf_manage_list")]
    # Bind controls to the displayed post/revision, not merely shared user_data.
    rows = [(label, action if action == "pf_manage_list" else
             f"{action}|{key}|{post.get('revision', 0)}") for label, action in rows]
    values = "\n".join(f"{label}: {post['values'].get(field, '—')}" for field, label in FIELDS.items())
    legacy = "\nДля старого поста перед правкой полей задайте текст с переменными. Можно переслать исходный пост, чтобы сохранить его оформление и фото." if not post.get("template") else ""
    await message.reply_text(f"{notice}\nПрофит {key}\n{values}\nСтатус: {STATUSES.get(post.get('status'), STATUSES['waiting'])}"
                             + ("\n⚠️ Требуется обновление канала" if post.get("pending_sync") else "") + legacy,
                             reply_markup=keyboard(rows))


async def callback(q, context):
    from profits import get, state
    action = q.data
    if "|" in action:
        action, key, revision = action.split("|")
        p = context.user_data.get("profit_flow", {})
        post = find(get(), key)
        if not post: return
        if p.get("key") != key or p.get("mode") != "manage_post" or str(post.get("revision", 0)) != revision:
            await show_post(q.message, context, key, "Открыты актуальные данные. Повторите действие.")
            return
    elif action not in ("pf_manage_list", "pf_manage_all", "pf_manage_search") and not action.startswith(("pf_manage_page_", "pf_manage_open_")):
        return
    if action in ("pf_manage_list", "pf_manage_all") or action.startswith("pf_manage_page_"):
        if action == "pf_manage_all": context.user_data.pop("profit_search", None)
        page = int(action.rsplit("_", 1)[1]) if action.startswith("pf_manage_page_") else 0
        await show_list(q.message, context, page)
        return
    if action == "pf_manage_search":
        state(context, mode="manage_search")
        await q.message.reply_text("Введите ник, сервис, номер сообщения, ссылку на пост или перешлите его из канала с видимым источником.")
        return
    if action.startswith("pf_manage_open_"):
        await show_post(q.message, context, action[len("pf_manage_open_"):])
        return
    p = context.user_data.get("profit_flow", {})
    if p.get("mode") != "manage_post": return
    data = get(); post = find(data, p["key"])
    if not post: return
    if post.get("revision", 0) != p["revision"]:
        await show_post(q.message, context, p["key"], "Профит уже изменён. Проверьте свежие данные.")
        return
    if action.startswith("pf_manage_status_"):
        status = action[len("pf_manage_status_"):]
        if status not in STATUSES: return
        post["status"] = status
        persist_change(data, post)
        notice = await sync_saved(context.bot, p["key"])
        await show_post(q.message, context, p["key"], notice)
    elif action == "pf_manage_sync":
        post["pending_sync"] = True
        from profits import save
        save(data)
        await show_post(q.message, context, p["key"], await sync_saved(context.bot, p["key"]))
    elif action == "pf_manage_body" or action.startswith("pf_manage_field_"):
        field = "body" if action == "pf_manage_body" else action[len("pf_manage_field_"):]
        if field not in (*FIELDS, "body"): return
        if field != "body" and not post.get("template"):
            await q.message.reply_text("Сначала задайте текст поста с переменными через «Текст / шаблон поста».")
            return
        if field != "body" and "{" + field + "}" not in post["template"]:
            await q.message.reply_text("В тексте нет переменной {" + field + "}. Сначала добавьте её через «Текст / шаблон поста», чтобы правка была видна в канале.")
            return
        p.update(mode="manage_input", field=field)
        await q.message.reply_text("Пришлите текст поста. Переменные: {worker}, {service}, {tp}, {curator}, {amount}, {share}. Фото исходного поста сохранится. Для старого фото-поста сначала перешлите оригинал через поиск."
                                   if field == "body" else f"Введите новое значение: {FIELDS[field]}. Изменение доли/воркера скорректирует топ только на разницу.")


async def input_message(message, context):
    from profits import get, save, formatted_html, render
    p = context.user_data["profit_flow"]
    text = (message.text or "").strip()
    data = get()
    if p["mode"] == "manage_search":
        origin = getattr(message, "forward_origin", None)
        chat = getattr(origin, "chat", None)
        key = f"{chat.id}:{origin.message_id}" if chat and getattr(origin, "message_id", None) else None
        match = re.search(r"t\.me/(?:c/(-?\d+)|([A-Za-z0-9_]+))/(\d+)", text)
        if match:
            channel = int("-100" + match[1]) if match[1] else (await context.bot.get_chat("@"+match[2])).id
            key = f"{channel}:{match[3]}"
        if key:
            post = find(data, key)
            if not post:
                await message.reply_text("Этого поста нет в истории бота. Начисления и данные не изменены.")
                return
            if chat:
                # Capture exact original HTML/photo, without guessing template substitutions.
                post["template"] = formatted_html(message)
                post["photo"] = message.photo[-1].file_id if message.photo else None
                post["revision"] = post.get("revision", 0) + 1
                save(data)
            await show_post(message, context, key)
        elif text:
            context.user_data["profit_search"] = text
            await show_list(message, context)
        return
    if p["mode"] != "manage_input": return
    post = find(data, p["key"])
    if not post: return
    if post.get("revision", 0) != p["revision"]:
        await show_post(message, context, p["key"], "Данные изменены другим администратором. Повторите правку.")
        return
    values = deepcopy(post["values"])
    field = p["field"]
    try:
        if field == "body":
            body = formatted_html(message)
            if not body: raise ValueError("Пришлите текст.")
            post["template"] = body
        elif field in ("amount", "share"):
            if not text.isdecimal() or not 0 < int(text) <= 10**15:
                raise ValueError("Введите положительное целое число до 10¹⁵.")
            values[field] = int(text)
            if int(values["share"]) > int(values["amount"]):
                raise ValueError("Доля воркера не может превышать сумму.")
        else:
            if not text or len(text) > 100: raise ValueError("Введите значение до 100 символов.")
            values[field] = "@" + text.lstrip("@#") if field == "worker" else text
        render({"body": post["template"], "photo": post.get("photo")}, values)
    except ValueError as exc:
        await message.reply_text(str(exc)); return
    # Freeze the edit before awaiting network calls. Retries only repeat channel sync.
    persist_change(data, post, values)
    p["mode"] = "manage_post"
    await show_post(message, context, p["key"], await sync_saved(context.bot, p["key"]))

