"""MTR STATS profit management."""
import html, json, os, re
from pathlib import Path
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler, filters

PATH=Path("/data/profits.json") if Path("/data").is_dir() else Path("profits.json")
LABEL={"service":"Сервисы","worker":"Воркеры","tp":"ТП","curator":"Кураторы"}
DEFAULT="🦅 <b>НОВЫЙ ПРОФИТ</b>\n\n👤 Воркер: {worker}\n🌳 ТП: {tp}\n👑 Куратор: {curator}\n💵 Сумма: {amount} ₽\n💸 Доля воркера: {share} ₽\n\n❔ Сервис: {service}"
def get():
    from work import recover_profit_transaction
    recover_profit_transaction()
    try:d=json.loads(PATH.read_text(encoding="utf8"))
    except FileNotFoundError:d={}
    if not isinstance(d,dict):raise ValueError("Profit data must be an object")
    for key in (*LABEL,"channels","templates","history"):d.setdefault(key,[])
    if any(not isinstance(d[key],list) for key in (*LABEL,"channels","templates","history")):
        raise ValueError("Invalid profit collections; refusing to overwrite data")
    return d
def save(d):
    PATH.parent.mkdir(parents=True,exist_ok=True)
    temp=PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding="utf8")
    os.replace(temp,PATH)
def keyboard(rows):return K([[B(name,callback_data=key)] for name,key in rows])
def menu():return keyboard([(name,"pf_list_"+key) for key,name in LABEL.items()]+[("📣 Каналы","pf_channels"),("🧩 Шаблоны","pf_templates"),("🚀 Закинуть профит","pf_new"),("📚 Опубликованные профиты","pf_manage_all"),("⬅️ Админка","pf_back")])
def listing(key):
    return keyboard([(f"🗑 {value}",f"pf_del_{key}_{i}") for i,value in enumerate(get()[key])]+[("➕ Добавить","pf_add_"+key),("⬅️ Назад","pf_home")])
def channels():return keyboard([(f"🗑 {x['title']}",f"pf_cdel_{i}") for i,x in enumerate(get()["channels"])]+[("➕ Добавить","pf_cadd"),("⬅️ Назад","pf_home")])
def templates():return keyboard([(x["name"],f"pf_template_{i}") for i,x in enumerate(get()["templates"])]+[("➕ Новый шаблон","pf_tadd"),("⬅️ Назад","pf_home")])
def state(c,**values):
    c.user_data.pop("awaiting",None)
    c.user_data["profit_flow"]=values
def clear(c):c.user_data.pop("profit_flow",None)
def render(template,values):
    text=template["body"]
    for key in ("service","worker","tp","curator","amount","share"):
        text=text.replace("{"+key+"}",html.escape(str(values.get(key,"—"))))
    limit=1024 if values.get("_photo") or template.get("photo") else 4096
    if len(re.sub(r"<[^>]*>", "", text))>limit:raise ValueError(f"Текст слишком длинный для публикации с фото (лимит {limit})")
    return text

def formatted_html(message):
    """Serialize Telegram entities, including custom premium emoji, to Bot API HTML."""
    raw = message.caption if message.caption is not None else (message.text or "")
    entities = message.caption_entities if message.caption is not None else message.entities
    encoded = raw.encode("utf-16-le")
    def chunk(a, b):
        return encoded[a*2:b*2].decode("utf-16-le")
    total = len(encoded)//2
    boundaries = {0,total}
    usable=[]
    for e in entities or []:
        if e.offset < 0 or e.offset+e.length > total: continue
        usable.append(e)
        boundaries.update((e.offset,e.offset+e.length))
    points=sorted(boundaries)
    output=[]
    for a,b in zip(points,points[1:]):
        fragment=html.escape(chunk(a,b))
        active=[e for e in usable if e.offset<=a and e.offset+e.length>=b]
        active.sort(key=lambda e:(e.offset,-e.length))
        for e in reversed(active):
            kind=str(e.type)
            if kind=="custom_emoji":
                fragment=f'<tg-emoji emoji-id="{e.custom_emoji_id}">{fragment}</tg-emoji>'
            elif kind in ("bold","italic","underline","strikethrough","spoiler","code","pre"):
                tag={"bold":"b","italic":"i","underline":"u","strikethrough":"s","spoiler":"tg-spoiler","code":"code","pre":"pre"}[kind]
                fragment=f"<{tag}>{fragment}</{tag}>"
            elif kind=="text_link" and e.url:
                fragment=f'<a href="{html.escape(e.url,quote=True)}">{fragment}</a>'
        output.append(fragment)
    return "".join(output)

async def start(update,context):
    if update.effective_user.id not in context.bot_data["profit_admins"]:return
    if update.effective_chat.type != "private":return
    context.user_data.pop("awaiting",None)
    clear(context)
    await update.message.reply_text("📊 MTR STATS · Профиты",reply_markup=menu())
async def next_field(message,context):
    p=context.user_data["profit_flow"];d=get()
    if "fields" not in p:
        t=d["templates"][p["template"]]
        p["fields"]=["service","worker"]+(["tp"] if t["tp"] else [])+(["curator"] if t["curator"] else [])+["amount","share"]
        for field in ("tp","curator"):
            if field not in p["fields"]:p["values"][field]="—"
    if p["step"]>=len(p["fields"]):
        p["mode"]="photo_choice"
        await message.reply_text("🖼 Добавить фото к этому профиту? Можно отправить фото сейчас или использовать фото из шаблона.",reply_markup=keyboard([("📷 Фото из шаблона / без фото","pf_preview"),("❌ Отмена","pf_cancel")]))
        return
    field=p["fields"][p["step"]]
    if field in ("amount","share"):
        p["mode"]="number"
        await message.reply_text("Введите "+("общую сумму" if field=="amount" else "долю воркера")+" (целое число):")
    else:
        p["mode"]="select"
        rows=[(value,f"pf_select_{i}") for i,value in enumerate(d[field])]
        if field in ("tp","curator"):rows.append(("⏭ Без "+field,"pf_skip"))
        rows.append(("❌ Отмена","pf_cancel"))
        await message.reply_text("Выберите "+LABEL[field]+":",reply_markup=keyboard(rows))
async def callback(update,context):
    q=update.callback_query
    # Channel buttons are indicators, never an administration endpoint.
    if q.data.startswith("pf_status_"):
        await q.answer();return
    if q.from_user.id not in context.bot_data["profit_admins"]:
        await q.answer();return
    if q.message.chat.type != "private":
        await q.answer();return
    await q.answer()
    if q.data.startswith("pf_manage_"):
        from profit_admin import callback as manage_callback
        await manage_callback(q,context);return
    action=q.data;d=get();p=context.user_data.get("profit_flow",{})
    if action=="pf_home":
        clear(context);await q.edit_message_text("📊 MTR STATS · Профиты",reply_markup=menu())
    elif action=="pf_back":
        clear(context)
        from work import admin_main_keyboard
        await q.edit_message_text("⚙️ Админ-панель",reply_markup=admin_main_keyboard())
    elif action.startswith("pf_list_"):
        key=action[len("pf_list_"):]
        clear(context);await q.edit_message_text(LABEL[key]+" (нажмите, чтобы удалить):",reply_markup=listing(key))
    elif action.startswith("pf_add_"):
        state(context,mode="add",kind=action[len("pf_add_"):])
        await q.edit_message_text("Отправьте название или @username:")
    elif action.startswith("pf_del_"):
        key,index=action[len("pf_del_"):].rsplit("_",1);index=int(index)
        if index<len(d[key]):d[key].pop(index);save(d)
        await q.edit_message_text(LABEL[key],reply_markup=listing(key))
    elif action=="pf_channels":
        clear(context);await q.edit_message_text("📣 Каналы:",reply_markup=channels())
    elif action=="pf_cadd":
        state(context,mode="channel")
        await q.edit_message_text("Перешлите сообщение из канала с видимым источником либо отправьте числовой ID (-100...). Добавьте бота администратором с правом публикации.")
    elif action.startswith("pf_cdel_"):
        index=int(action[len("pf_cdel_"):])
        if index<len(d["channels"]):d["channels"].pop(index);save(d)
        await q.edit_message_text("📣 Каналы:",reply_markup=channels())
    elif action=="pf_templates":
        clear(context);await q.edit_message_text("🧩 Шаблоны:",reply_markup=templates())
    elif action=="pf_tadd":
        state(context,mode="template_name");await q.edit_message_text("Введите название анкеты, например Drag · с ТП · без куратора:")
    elif action.startswith("pf_template_"):
        index=int(action[len("pf_template_"):])
        if index>=len(d["templates"]):return
        state(context,mode="template_edit",index=index)
        await q.edit_message_text(d["templates"][index]["name"],reply_markup=keyboard([("✏️ Изменить оформление","pf_tbody"),("🖼 Установить фото шаблона","pf_tphoto"),("🗑 Удалить шаблон","pf_tdelete"),("⬅️ Назад","pf_templates")]))
    elif action=="pf_tbody":
        if p.get("mode")!="template_edit":return
        p["mode"]="template_edit_body"
        await q.edit_message_text("Пришлите новый текст анкеты с премиум-эмодзи. Переменные: {worker}, {tp}, {curator}, {amount}, {share}, {service}.")
    elif action=="pf_tphoto":
        if p.get("mode")!="template_edit":return
        p["mode"]="template_photo"
        await q.edit_message_text("📷 Пришлите фото. Оно будет использоваться по умолчанию во всех профитах этого шаблона. Для удаления отправьте слово УДАЛИТЬ.")
    elif action=="pf_tdelete":
        if p.get("mode")!="template_edit":return
        index=p["index"]
        if index<len(d["templates"]):d["templates"].pop(index);save(d)
        clear(context);await q.edit_message_text("Шаблон удалён.",reply_markup=templates())
    elif action=="pf_new":
        clear(context)
        if not d["channels"] or not d["templates"] or not d["service"] or not d["worker"]:
            await q.edit_message_text("Сначала добавьте канал, шаблон, сервис и воркера.",reply_markup=menu());return
        await q.edit_message_text("Выберите анкету:",reply_markup=keyboard([(x["name"],f"pf_use_{i}") for i,x in enumerate(d["templates"])]))
    elif action.startswith("pf_use_"):
        index=int(action[len("pf_use_"):])
        if index>=len(d["templates"]):return
        state(context,mode="destination",template=index,values={})
        await q.edit_message_text("Выберите канал:",reply_markup=keyboard([(x["title"],f"pf_dest_{i}") for i,x in enumerate(d["channels"])]))
    elif action.startswith("pf_dest_"):
        if p.get("mode")!="destination":return
        p["channel"]=int(action[len("pf_dest_"):]);p["step"]=0
        await next_field(q.message,context)
    elif action.startswith("pf_select_"):
        if p.get("mode")!="select":return
        field=p["fields"][p["step"]];index=int(action[len("pf_select_"):])
        if index>=len(d[field]):return
        p["values"][field]=d[field][index];p["step"]+=1
        await next_field(q.message,context)
    elif action=="pf_skip":
        if p.get("mode")!="select":return
        field=p["fields"][p["step"]]
        if field not in ("tp","curator"):return
        p["values"][field]="—";p["step"]+=1
        await next_field(q.message,context)
    elif action=="pf_preview":
        if p.get("mode")!="photo_choice":return
        await preview(q.message,context)
    elif action=="pf_cancel":
        clear(context)
        if q.message.photo:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("Отменено.",reply_markup=menu())
        else:await q.edit_message_text("Отменено.",reply_markup=menu())
    elif action=="pf_publish":
        if p.get("mode")!="confirm":return
        p["mode"]="publishing"
        try:
            template=d["templates"][p["template"]];channel=d["channels"][p["channel"]]
            # Copy the exact preview, preserving its photo, formatting and custom emoji entities.
            # The inline confirmation keyboard is not copied by Telegram.
            if q.message.message_id != p.get("preview_id") or q.message.chat_id != p.get("preview_chat"):
                await q.message.reply_text("Предпросмотр устарел. Создайте профит заново.")
                return
            from profit_admin import status_keyboard
            sent=await context.bot.copy_message(
                chat_id=channel["id"],
                from_chat_id=p["preview_chat"],
                message_id=p["preview_id"],
                reply_markup=status_keyboard({"status":"waiting"}),
            )
        except Exception:
            await q.message.reply_text("⚠️ Результат публикации неизвестен. Проверьте канал перед новой отправкой, чтобы не создать дубликат.");return
        p["mode"]="sent"
        try:
            from work import load_data,commit_profit_data
            from profit_admin import delta_top,worker_key,BUTTONS_VERSION
            top=delta_top(load_data(),None,p["values"]);worker=worker_key(top,p["values"]["worker"])
            d=get();d["history"].append({"channel":channel["id"],"message":sent.message_id,
                "values":dict(p["values"]),"template":template["body"],
                "photo":p["values"].get("_photo") or template.get("photo"),
                "status":"waiting","revision":0,"buttons_version":BUTTONS_VERSION});commit_profit_data(d,top)
            clear(context)
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text(f"✅ Опубликовано! {worker}: {top[worker]:,} ₽",reply_markup=menu())
        except Exception:
            await q.message.reply_text("⚠️ Пост опубликован, но возникла ошибка сохранения. Не отправляйте его повторно; проверьте хранилище.")
async def input_message(update,context):
    if update.effective_user.id not in context.bot_data["profit_admins"]:return
    if update.effective_chat.type != "private":return
    p=context.user_data.get("profit_flow")
    if not p:return
    message=update.message;text=(message.text or "").strip();d=get();mode=p["mode"]
    if mode.startswith("manage_"):
        from profit_admin import input_message as manage_input
        await manage_input(message,context);return
    if mode=="add":
        key=p["kind"]
        if not text or len(text)>100:await message.reply_text("Введите название до 100 символов.");return
        if key=="worker":text="@"+text.lstrip("@")
        if text not in d[key]:d[key].append(text);save(d)
        clear(context);await message.reply_text("✅ Добавлено.",reply_markup=listing(key))
    elif mode=="channel":
        origin=getattr(message,"forward_origin",None)
        chat=getattr(origin,"chat",None)
        identifier=chat.id if chat and chat.type=="channel" else None
        if not identifier:
            try:identifier=int(text)
            except ValueError:await message.reply_text("Нужен ID канала либо пересланный пост.");return
        try:
            channel=await context.bot.get_chat(identifier)
            member=await context.bot.get_chat_member(channel.id,context.bot.id)
            if channel.type!="channel" or member.status not in ("administrator","creator") or (member.status=="administrator" and not member.can_post_messages):
                await message.reply_text("Бот должен иметь право публикации в канале.");return
        except Exception:await message.reply_text("Не удалось проверить канал. Проверьте ID канала и права бота.");return
        if not any(x["id"]==channel.id for x in d["channels"]):
            d["channels"].append({"id":channel.id,"title":channel.title});save(d)
        clear(context);await message.reply_text("✅ Канал добавлен.",reply_markup=menu())
    elif mode=="template_name":
        if not text or len(text)>80:await message.reply_text("Введите название до 80 символов.");return
        p["name"]=text;p["mode"]="template_flags"
        await message.reply_text("Будут ТП и куратор? Два слова да/нет. Например: да нет")
    elif mode=="template_flags":
        values=text.lower().split()
        if len(values)!=2 or any(v not in ("да","нет") for v in values):
            await message.reply_text("Пример: да нет");return
        p["tp"]=values[0]=="да";p["curator"]=values[1]=="да";p["mode"]="template_body"
        await message.reply_text("Пришлите текст анкеты с премиум-эмодзи. Переменные: {worker}, {tp}, {curator}, {amount}, {share}, {service}. Или отправьте «по умолчанию».")
    elif mode in ("template_body","template_edit_body"):
        body=DEFAULT if text.lower()=="по умолчанию" else formatted_html(message)
        if not body:await message.reply_text("Отправьте текст.");return
        if mode=="template_body":d["templates"].append({"name":p["name"],"tp":p["tp"],"curator":p["curator"],"body":body,"photo":message.photo[-1].file_id if message.photo else None})
        else:
            d["templates"][p["index"]]["body"]=body
            if message.photo:d["templates"][p["index"]]["photo"]=message.photo[-1].file_id
        save(d);clear(context);await message.reply_text("✅ Шаблон сохранён.",reply_markup=menu())
    elif mode=="template_photo":
        if p["index"]>=len(d["templates"]):return
        if message.photo:d["templates"][p["index"]]["photo"]=message.photo[-1].file_id
        elif text.upper()=="УДАЛИТЬ":d["templates"][p["index"]]["photo"]=None
        else:await message.reply_text("Пришлите фото или слово УДАЛИТЬ.");return
        save(d);clear(context);await message.reply_text("✅ Фото шаблона обновлено.",reply_markup=menu())
    elif mode=="photo_choice":
        if not message.photo:
            await message.reply_text("Пришлите фото или нажмите кнопку «Фото из шаблона / без фото».");return
        p["values"]["_photo"]=message.photo[-1].file_id
        await preview(message,context)
    elif mode=="number":
        if not text.isdigit() or int(text)<=0:
            await message.reply_text("Введите положительное целое число.");return
        field=p["fields"][p["step"]];value=int(text)
        if field=="share" and value>p["values"]["amount"]:
            await message.reply_text("Доля воркера не может превышать сумму.");return
        p["values"][field]=value;p["step"]+=1
        await next_field(message,context)
async def preview(message,context):
    p=context.user_data["profit_flow"];t=get()["templates"][p["template"]]
    try:
        body=render(t,p["values"])
        photo=p["values"].get("_photo") or t.get("photo")
        actions=keyboard([("✅ Опубликовать и начислить","pf_publish"),("❌ Отмена","pf_cancel")])
        if photo:sent=await message.reply_photo(photo,caption=body,parse_mode="HTML",reply_markup=actions)
        else:sent=await message.reply_text(body,parse_mode="HTML",reply_markup=actions)
        p["preview_id"]=sent.message_id
        p["preview_chat"]=sent.chat_id
        p["mode"]="confirm"
    except ValueError as exc:await message.reply_text(f"Ошибка оформления: {exc}")
    except Exception:await message.reply_text("Не удалось создать предпросмотр. Проверьте оформление и доступность фото.")

def register(app,admin_ids):
    app.bot_data["profit_admins"]=admin_ids
    app.add_handler(CommandHandler("profits",start))
    app.add_handler(CallbackQueryHandler(callback,pattern=r"^pf_"))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND,input_message),group=-1)


