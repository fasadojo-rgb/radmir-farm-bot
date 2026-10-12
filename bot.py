"""Radmir Farm Bot v3.3.2 deployment diagnostic. Python 3.10+, aiogram 3.x, SQLite."""
import asyncio
import logging
import os
import sqlite3
import time
import html
from collections import Counter
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))
TOKEN = os.getenv('BOT_TOKEN', '')
ALLOWED_ID = int(os.getenv('ALLOWED_USER_ID') or 0)
DB_PATH = os.getenv('DB_PATH') or str(Path(__file__).with_name('farm.sqlite3'))
MAX_GARDENS = 6
CAPACITY = 20
WARN = 3 * 60
CROPS = {
    'tomato': ('🍅 Помидоры', 120, 20), 'cucumber': ('🥒 Огурцы', 120, 20),
    'cannabis': ('🌿 Марихуана', 180, 30), 'potato': ('🥔 Картофель', 170, 50),
    'beet': ('🫜 Свёкла', 110, 35), 'pepper': ('🌶️ Перец', 170, 35),
    'eggplant': ('🍆 Баклажан', 200, 25), 'cabbage': ('🥬 Капуста', 110, 20),
    'carrot': ('🥕 Морковь', 155, 50),
}
# Prices per unit (packs contain 20, except shovel); vegetable selling price is known for pepper only.
PRICES = {'seed_tomato': 875/20, 'seed_cucumber': 875/20,
          **{f'seed_{k}': 3500/20 for k in ('potato','beet','pepper','eggplant','cabbage','carrot')},
          'lime': 4375/20, 'compost': 5250/20, 'herbicide': 7000/20, 'shovel': 9625/20}
# Other seed prices are not verified; don't silently invent a price.

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA busy_timeout=5000')


def initialize():
    # Upgrade old v1 database: old plants table restricted garden to 1..3.
    existing = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='plants'").fetchone()
    if existing:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='plants'").fetchone()[0]
        if 'garden BETWEEN 1 AND 3' in sql:
            with db:
                db.execute('ALTER TABLE plants RENAME TO plants_v1')
                db.execute('''CREATE TABLE plants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    garden INTEGER NOT NULL CHECK(garden BETWEEN 1 AND 6), crop TEXT NOT NULL,
                    planted_at INTEGER NOT NULL, watered_at INTEGER NOT NULL,
                    warning_sent_for INTEGER NOT NULL DEFAULT 0, due_sent_for INTEGER NOT NULL DEFAULT 0,
                    harvest_sent INTEGER NOT NULL DEFAULT 0)''')
                db.execute('''INSERT INTO plants(id,user_id,garden,crop,planted_at,watered_at,
                    warning_sent_for,due_sent_for,harvest_sent)
                    SELECT id,user_id,garden,crop,planted_at,watered_at,
                    warning_sent_for,due_sent_for,harvest_sent FROM plants_v1''')
                db.execute('DROP TABLE plants_v1')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS plants (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        garden INTEGER NOT NULL CHECK(garden BETWEEN 1 AND 6), crop TEXT NOT NULL,
        planted_at INTEGER NOT NULL, watered_at INTEGER NOT NULL,
        warning_sent_for INTEGER NOT NULL DEFAULT 0, due_sent_for INTEGER NOT NULL DEFAULT 0,
        harvest_sent INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS ix_plants_user_garden ON plants(user_id,garden);
    CREATE TABLE IF NOT EXISTS gardens (user_id INTEGER NOT NULL, num INTEGER NOT NULL,
        PRIMARY KEY(user_id,num));
    CREATE TABLE IF NOT EXISTS plant_extras (plant_id INTEGER PRIMARY KEY,
        compost INTEGER NOT NULL DEFAULT 0, herbicide INTEGER NOT NULL DEFAULT 0, carrot_bonus INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS water_actions (id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, created_at INTEGER NOT NULL, undone INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS water_action_items (
        action_id INTEGER NOT NULL, plant_id INTEGER NOT NULL,
        prev_watered_at INTEGER NOT NULL, new_watered_at INTEGER NOT NULL,
        PRIMARY KEY(action_id,plant_id));
    CREATE TABLE IF NOT EXISTS harvest_log (user_id INTEGER, garden INTEGER, crop TEXT,
        quantity INTEGER, value INTEGER, harvested_at INTEGER);
    CREATE TABLE IF NOT EXISTS settings (user_id INTEGER PRIMARY KEY, shovel_uses INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS stock_moves (id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, sku TEXT NOT NULL, quantity INTEGER NOT NULL,
        paid REAL NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS ix_stock_user_sku ON stock_moves(user_id,sku);
    CREATE TABLE IF NOT EXISTS unit_prices (user_id INTEGER NOT NULL, sku TEXT NOT NULL, price REAL NOT NULL, PRIMARY KEY(user_id,sku));
    CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, quantity INTEGER NOT NULL, total REAL NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS pending_inputs (user_id INTEGER PRIMARY KEY, action TEXT NOT NULL,
        sku TEXT NOT NULL DEFAULT '', amount INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS harvest_details (id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, garden INTEGER NOT NULL, crop TEXT NOT NULL,
        yield_quantity INTEGER NOT NULL, harvested_at INTEGER NOT NULL);
    ''')
    cols=[r[1] for r in db.execute('PRAGMA table_info(plant_extras)')]
    if 'carrot_bonus' not in cols:
        db.execute('ALTER TABLE plant_extras ADD COLUMN carrot_bonus INTEGER NOT NULL DEFAULT 0')
        db.execute('''INSERT OR IGNORE INTO plant_extras(plant_id)
            SELECT id FROM plants WHERE crop='pepper' AND EXISTS
            (SELECT 1 FROM plants q WHERE q.user_id=plants.user_id AND q.garden=plants.garden AND q.crop='carrot')''')
        db.execute('''UPDATE plant_extras SET carrot_bonus=1 WHERE plant_id IN
            (SELECT id FROM plants p WHERE p.crop='pepper' AND EXISTS
            (SELECT 1 FROM plants c WHERE c.user_id=p.user_id AND c.garden=p.garden AND c.crop='carrot'))''')
    db.execute('CREATE TABLE IF NOT EXISTS custom_stock_items (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)')
    db.execute('CREATE TABLE IF NOT EXISTS hidden_stock_items (user_id INTEGER NOT NULL, sku TEXT NOT NULL, PRIMARY KEY(user_id,sku))')
    cols={r[1] for r in db.execute('PRAGMA table_info(plants)')}
    if 'grown_seconds' not in cols:
        db.execute('ALTER TABLE plants ADD COLUMN grown_seconds INTEGER NOT NULL DEFAULT 0')
        db.execute('ALTER TABLE plants ADD COLUMN growth_synced_at INTEGER NOT NULL DEFAULT 0')
        # Historic missed-water timestamps are unavailable: grant at most one interval.
        for crop,(_,_,interval) in CROPS.items():
            db.execute('UPDATE plants SET grown_seconds=MAX(0,MIN(watered_at-planted_at,?)),growth_synced_at=watered_at WHERE crop=?',(interval*60,crop))
    cols={r[1] for r in db.execute('PRAGMA table_info(water_action_items)')}
    for col in ('prev_grown_seconds','prev_growth_synced_at'):
        if col not in cols:
            db.execute(f'ALTER TABLE water_action_items ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0')
    db.execute('CREATE INDEX IF NOT EXISTS ix_plants_user_garden ON plants(user_id,garden)')
    db.commit()

initialize()

def clock(): return int(time.time())
def fmtmoney(v): return f'{v:,.2f}'.replace(',', ' ').replace('.', ',').removesuffix(',00') + ' ₽'
def duration(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m = (rem + 59)//60
    if m == 60:
        h += 1; m = 0
    return f'{h} ч. {m} мин.' if h else f'{m} мин.'
def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t,d in r] for r in rows])
def ensure(user):
    if not db.execute('SELECT 1 FROM gardens WHERE user_id=? LIMIT 1', (user,)).fetchone():
        db.execute('INSERT INTO gardens VALUES (?,1)', (user,))
    db.execute('INSERT OR IGNORE INTO settings(user_id) VALUES(?)', (user,))
    db.commit()
def garden_nums(user):
    ensure(user)
    return [r[0] for r in db.execute('SELECT num FROM gardens WHERE user_id=? ORDER BY num',(user,))]
def valid_garden(user,g): return g in garden_nums(user)
def get_plants(user,g=None):
    if g is None:
        return db.execute('SELECT * FROM plants WHERE user_id=? ORDER BY garden,crop,id',(user,)).fetchall()
    return db.execute('SELECT * FROM plants WHERE user_id=? AND garden=? ORDER BY crop,id',(user,g)).fetchall()
def get_plant(user,pid): return db.execute('SELECT * FROM plants WHERE id=? AND user_id=?',(pid,user)).fetchone()
def due_at(p): return p['watered_at']+CROPS[p['crop']][2]*60
def growth_seconds(p, now=None):
    now=clock() if now is None else now
    start=max(p['watered_at'],p['growth_synced_at'])
    return min(CROPS[p['crop']][1]*60,p['grown_seconds']+max(0,min(now,due_at(p))-start))
def is_ready(p): return growth_seconds(p)>=CROPS[p['crop']][1]*60
def progress(p): return min(100,int(100*growth_seconds(p)/(CROPS[p['crop']][1]*60)))
def average_progress(ps): return round(sum(progress(p) for p in ps)/len(ps)) if ps else 0
def garden_summary(user,g):
    ps=get_plants(user,g); count=len(ps); names=Counter(p['crop'] for p in ps)
    result=[f'🌱 <b>Огород №{g}</b>', f'🪴 Заполнено: {count}/{CAPACITY} ({count*5}%)',
            f'📈 Средний рост: {average_progress(ps)}%']
    result += [f'{CROPS[c][0]} ×{q}' for c,q in names.items()]
    active=[p for p in ps if not is_ready(p)]
    if active:
        due=min(due_at(p) for p in active)
        result.append('💧 Полив: '+('просрочен' if due<=clock() else 'через '+duration(due-clock())))
    if any(is_ready(p) for p in ps):
        result.append(f'✅ Созрело: {sum(is_ready(p) for p in ps)}')
    return '\n'.join(result)
def bar(value, blocks=10):
    filled = min(blocks, max(0, round(value * blocks / 100)))
    return '🟩' * filled + '⬜' * (blocks - filled)


def dashboard(user):
    gardens = garden_nums(user)
    now = clock()
    total = len(get_plants(user))
    lines = [
        '🌾 <b>RADMIR FARM | МОИ ОГОРОДЫ</b>',
        '━━━━━━━━━━━━━━━━━━━━',
        f'🏡 Огородов: <b>{len(gardens)}/{MAX_GARDENS}</b>  •  🌱 Растений: <b>{total}/{len(gardens)*CAPACITY}</b>',
        ''
    ]
    for g in gardens:
        ps = get_plants(user, g)
        fill = round(len(ps) * 100 / CAPACITY)
        avg = average_progress(ps)
        active = [p for p in ps if not is_ready(p)]
        ready = len(ps) - len(active)
        if active:
            nearest = min(due_at(p) for p in active)
            watering = ('🚨 <b>Полив просрочен!</b>' if nearest <= now
                        else '💧 Полив через <b>' + duration(nearest - now) + '</b>')
        elif ready:
            watering = '✅ Все растения созрели'
        else:
            watering = '💧 Нет активных растений'
        lines.extend([
            f'🌿 <b>Огород №{g}</b>  •  <b>{len(ps)}/{CAPACITY}</b> мест ({fill}%)',
            f'📈 Рост: <b>{avg}%</b>  {bar(avg)}',
            watering,
        ])
        if ready:
            lines.append(f'🎉 Готово к сбору: <b>{ready}</b>')
        lines.append('')
    if total:
        active_all = [p for p in get_plants(user) if not is_ready(p)]
        due_count = sum(due_at(p)<=now for p in active_all)
        if due_count:
            lines.append(f'⚠️ Нужен полив: <b>{due_count} кустов</b>')
    lines.append('👇 Выбери огород или действие ниже')
    return '\n'.join(lines)


def home_kb(user):
    rows = [[(f'🌿 Огород №{g}  •  {len(get_plants(user,g))}/20', f'g:{g}')]
            for g in garden_nums(user)]
    if len(rows)<MAX_GARDENS:
        rows.append([('➕ Добавить огород','newgarden')])
    rows += [
        [('💧 Полил ВСЕ огороды','askw:0:all')],
        [('↩️ Отменить полив','undoask'),('🔄 Обновить','refresh')],
        [('⭐ Шаблоны','templates'),('📦 Склад','stock')],
        [('💰 Финансы','finance'),('⚙️ Настройки','settings')],
    ]
    return keyboard(rows)


def settings_view(user):
    return ('⚙️ <b>НАСТРОЙКИ • RADMIR FARM</b>\n'
            '━━━━━━━━━━━━━━━━━━━━\n'
            '🏡 Максимум огородов: <b>6</b>\n'
            '🌱 Мест на огород: <b>20</b>\n'
            '⏰ Предупреждение: <b>за 3 минуты до полива</b>\n'
            '💧 Каждый куст имеет свой таймер\n'
            '🔄 Обновление главного меню — по кнопке\n\n'
            '📌 Для изменения цен материалов открой <b>Склад → Настроить цены</b>.\n'
            '🛡 Для защиты доступа можно задать ALLOWED_USER_ID в Secrets.')

def garden_kb(g):
    return keyboard([[('➕ Посадить',f'add:{g}'),('📋 Кусты',f'list:{g}:0')],
                     [('💧 Полил огород',f'askw:{g}:all'),('💧 По культуре',f'wc:{g}')],
                     [('✅ Собрать созревшие',f'askh:{g}')],
                     [('🧹 Очистить грядки',f'askclear:{g}')],
                     [('❌ Удалить огород',f'askdeletegarden:{g}')],
                     [('🏠 Главное меню','home')]])
def crop_list(user,g,page):
    ps=get_plants(user,g); max_page=max(0,(len(ps)-1)//8); page=max(0,min(page,max_page))
    rows=[]; lines=[garden_summary(user,g),'',f'Кусты, страница {page+1}/{max_page+1}:']
    for p in ps[page*8:(page+1)*8]:
        status=('✅ готов' if is_ready(p) else
                '🚨 полив!' if due_at(p)<=clock() else f'💧 {duration(due_at(p)-clock())}')
        lines.append(f'#{p["id"]} {CROPS[p["crop"]][0]} • рост {progress(p)}% • {status}')
        rows.append([(f'💧 #{p["id"]}',f'w:{p["id"]}'),(f'✅ #{p["id"]}',f'h:{p["id"]}')])
    nav=[]
    if page: nav.append(('◀',f'list:{g}:{page-1}'))
    if page<max_page: nav.append(('▶',f'list:{g}:{page+1}'))
    if nav: rows.append(nav)
    rows.append([('⬅️ Огород',f'g:{g}')])
    return '\n'.join(lines), keyboard(rows)
def add_plants(user,g,crop,qty):
    if not valid_garden(user,g) or crop not in CROPS or qty<1 or len(get_plants(user,g))+qty>20: return False
    t=clock()
    with db:
        db.executemany('INSERT INTO plants(user_id,garden,crop,planted_at,watered_at) VALUES (?,?,?,?,?)',
                       [(user,g,crop,t,t)]*qty)
        record_plant_costs(user,[crop]*qty)
        if crop=='carrot':
            db.execute('INSERT OR IGNORE INTO plant_extras(plant_id) SELECT id FROM plants WHERE user_id=? AND garden=? AND crop="pepper"',(user,g))
            db.execute('UPDATE plant_extras SET carrot_bonus=1 WHERE plant_id IN (SELECT id FROM plants WHERE user_id=? AND garden=? AND crop="pepper")',(user,g))
        elif crop=='pepper':
            if db.execute('SELECT 1 FROM plants WHERE user_id=? AND garden=? AND crop="carrot" LIMIT 1',(user,g)).fetchone():
                db.execute('INSERT OR IGNORE INTO plant_extras(plant_id) SELECT id FROM plants WHERE user_id=? AND garden=? AND crop="pepper"',(user,g))
                db.execute('UPDATE plant_extras SET carrot_bonus=1 WHERE plant_id IN (SELECT id FROM plants WHERE user_id=? AND garden=? AND crop="pepper")',(user,g))
    if len(get_plants(user,g))==20:
        mark_full_garden(user)
    return True
def mark_full_garden(user):
    with db:
        row=db.execute('SELECT shovel_uses FROM settings WHERE user_id=?',(user,)).fetchone()
        current=row['shovel_uses'] if row else 0
        db.execute('UPDATE settings SET shovel_uses=? WHERE user_id=?',(current+1,user))
        if (current+1)%20==0: spend_item(user,'shovel',1)
        spend_item(user,'herbicide',5)
def do_water(user,ps):
    # Save pre-action timestamps so the latest watering can be undone precisely.
    if not ps: return 0
    t=clock()
    with db:
        cur=db.execute('INSERT INTO water_actions(user_id,created_at) VALUES(?,?)',(user,t))
        action_id=cur.lastrowid
        db.executemany('''INSERT INTO water_action_items
            (action_id,plant_id,prev_watered_at,new_watered_at,prev_grown_seconds,prev_growth_synced_at)
            VALUES(?,?,?,?,?,?)''',[(action_id,p['id'],p['watered_at'],t,p['grown_seconds'],p['growth_synced_at']) for p in ps])
        db.executemany('''UPDATE plants SET watered_at=?,grown_seconds=?,growth_synced_at=?,warning_sent_for=0,due_sent_for=0
                          WHERE user_id=? AND id=?''',[(t,growth_seconds(p,t),t,user,p['id']) for p in ps])
    return len(ps)
def undo_water(user):
    action=db.execute('SELECT * FROM water_actions WHERE user_id=? AND undone=0 ORDER BY id DESC LIMIT 1',(user,)).fetchone()
    if not action: return 0
    items=db.execute('SELECT * FROM water_action_items WHERE action_id=?',(action['id'],)).fetchall()
    restored=0
    with db:
        for item in items:
            # Do not overwrite a newer watering action.
            cur=db.execute('''UPDATE plants SET watered_at=?,grown_seconds=?,growth_synced_at=?,warning_sent_for=0,due_sent_for=0
                WHERE user_id=? AND id=? AND watered_at=?''',
                (item['prev_watered_at'],item['prev_grown_seconds'],item['prev_growth_synced_at'],user,item['plant_id'],item['new_watered_at']))
            restored+=cur.rowcount
        db.execute('UPDATE water_actions SET undone=1 WHERE id=?',(action['id'],))
    return restored
STOCK_ITEMS = {
    'seed_pepper': '🌶️ Семена перца',
    'seed_carrot': '🥕 Семена моркови',
    'seed_tomato': '🍅 Семена помидора',
    'seed_cucumber': '🥒 Семена огурца',
    'seed_potato': '🥔 Семена картофеля',
    'seed_beet': '🫜 Семена свёклы',
    'seed_eggplant': '🍆 Семена баклажана',
    'seed_cabbage': '🥬 Семена капусты',
    'seed_cannabis': '🌿 Семена марихуаны',
    'lime': '🧪 Известь', 'compost': '🌿 Компост',
    'herbicide': '🧴 Гербицид', 'peat': '🪴 Торф',
    'shovel': '🪏 Лопата',
}
def stock_items(user):
    hidden={r[0] for r in db.execute('SELECT sku FROM hidden_stock_items WHERE user_id=?',(user,))}
    result={sku:name for sku,name in STOCK_ITEMS.items() if sku not in hidden}
    for r in db.execute('SELECT id,name FROM custom_stock_items WHERE user_id=? AND active=1 ORDER BY id',(user,)):
        result['custom_'+str(r['id'])]='📎 '+r['name']
    return result
def item_name(user,sku):
    return stock_items(user).get(sku,STOCK_ITEMS.get(sku,sku))
def valid_item(user,sku): return sku in stock_items(user)

def unit_price(user, sku):
    custom=db.execute('SELECT price FROM unit_prices WHERE user_id=? AND sku=?',(user,sku)).fetchone()
    return custom['price'] if custom else PRICES.get(sku)

def prices_menu(user):
    """Build the price editor using a single snapshot of available stock items."""
    items = list(stock_items(user).items())
    lines = ['⚙️ <b>НАСТРОЙКА ЦЕН</b>', '━━━━━━━━━━━━━━━━━━',
             'Выбери товар для изменения цены <b>за одну штуку</b>.',
             'При пополнении склада общая стоимость считается автоматически.\n']
    rows = []
    for sku, name in items:
        value = unit_price(user, sku)
        shown_price = money(value) if value is not None else 'не задана ⚠️'
        lines.append(f'{html.escape(str(name))}: <b>{shown_price}</b>')
        rows.append([(str(name)[:60], f'price:{sku}')])
    rows.append([('⬅️ Склад', 'stock')])
    return '\n'.join(lines), keyboard(rows)

def units(user,sku):
    return db.execute('SELECT COALESCE(SUM(quantity),0) FROM stock_moves WHERE user_id=? AND sku=?', (user,sku)).fetchone()[0]
def spend_item(user,sku,n):
    db.execute('INSERT INTO stock_moves(user_id,sku,quantity,paid,created_at,note) VALUES(?,?,?,?,?,?)',
               (user,sku,-n,0,clock(),'Использовано в огороде'))
def record_plant_costs(user,crops):
    for crop,n in Counter(crops).items():
        spend_item(user,'seed_'+crop,n)
    spend_item(user,'lime',len(crops))
    spend_item(user,'compost',len(crops))
    # Shovel use and 5 herbicides are recorded when the garden reaches 20 plants.
def plant_yield(user,p):
    if p['crop']!='pepper': return 0
    b=db.execute('SELECT carrot_bonus FROM plant_extras WHERE plant_id=?',(p['id'],)).fetchone()
    return 5+2+2*bool(b and b['carrot_bonus'])  # mandatory compost, historical carrot bonus

def purchased_total(user):
    return db.execute('SELECT COALESCE(SUM(paid),0) FROM stock_moves WHERE user_id=?',(user,)).fetchone()[0]
def sold_total(user):
    return db.execute('SELECT COALESCE(SUM(total),0) FROM sales WHERE user_id=?',(user,)).fetchone()[0]
def money(v): return fmtmoney(v)
def finance_menu(user):
    paid=purchased_total(user); sold=sold_total(user)
    ps=get_plants(user)
    remaining=sum(plant_yield(user,p) for p in ps)
    harvested=db.execute('SELECT COALESCE(SUM(yield_quantity),0) FROM harvest_details WHERE user_id=? AND crop="pepper"',(user,)).fetchone()[0]
    units_sold=db.execute('SELECT COALESCE(SUM(quantity),0) FROM sales WHERE user_id=?',(user,)).fetchone()[0]
    by_sku=db.execute('''SELECT sku,SUM(paid) AS total FROM stock_moves
                         WHERE user_id=? AND paid>0 GROUP BY sku ORDER BY total DESC''',(user,)).fetchall()
    lines=[f'  • {item_name(user,r["sku"])}: {money(r["total"])}' for r in by_sku]
    breakdown='\n'.join(lines) if lines else '  Пока не внесены'
    return (f'💰 <b>ФИНАНСЫ • RADMIR FARM</b>\n'
            f'━━━━━━━━━━━━━━━━━━\n'
            f'🔻 ГРЯЗНЫЙ МИНУС — закупки: <b>{money(paid)}</b>\n'
            f'💵 Продажи (получено): <b>{money(sold)}</b>\n'
            f'🟢 Чистый денежный итог: <b>{money(sold-paid)}</b>\n'
            f'━━━━━━━━━━━━━━━━━━\n'
            f'🌶️ Потенциально на растущих кустах: <b>{remaining} шт.</b>\n'
            f'💎 Прогноз выручки: <b>{money(remaining*1100)}</b>\n'
            f'📦 Собрано перца: {harvested} шт.\n'
            f'✅ Продано перца (внесено вручную): {units_sold} шт.\n'
            f'\n🧾 <b>Расходы по закупкам:</b>\n{breakdown}\n'
            f'\n<i>Денежный результат = зафиксированные продажи − реальные закупки. '
            f'Прогноз не считается полученными деньгами. '
            f'Неучтённые ранние покупки нужно внести вручную.</i>')
def stock_menu(user):
    lines=['📦 <b>СКЛАД И ОПТОВЫЕ ЗАКУПКИ</b>','━━━━━━━━━━━━━━━━━━']
    for sku,name in stock_items(user).items():
        n=units(user,sku)
        if n or sku in ('seed_pepper','seed_carrot','lime','compost','herbicide','shovel'):
            lines.append(f'{name}: <b>{n} шт.</b>'+(' ⚠️ нехватка / старые посадки' if n<0 else ''))
    lines.append('\n➕ Выбери материал и введи только количество. Стоимость рассчитается по сохранённой цене.\n'
                 '<i>Отрицательные остатки означают, что расход уже отмечен, но закупка не внесена.</i>')
    rows=[[(name,f'buy:{sku}')] for sku,name in stock_items(user).items()]
    rows += [[('➕ Новый товар','stockadd'),('🗑 Удалить товар','stockremove')], [('♻️ Вернуть скрытые','stockrestore')], [('⚙️ Настроить цены','prices')], [('💰 Финансы','finance'),('🏠 Меню','home')]]
    return '\n'.join(lines),keyboard(rows)

def state_set(user,action,sku='',amount=0):
    with db: db.execute('INSERT OR REPLACE INTO pending_inputs VALUES(?,?,?,?)',(user,action,sku,amount))
def state_clear(user):
    with db: db.execute('DELETE FROM pending_inputs WHERE user_id=?',(user,))

def archive_harvest(user,ps):
    for p in ps:
        db.execute('INSERT INTO harvest_details(user_id,garden,crop,yield_quantity,harvested_at) VALUES (?,?,?,?,?)',
                   (user,p['garden'],p['crop'],plant_yield(user,p),clock()))

async def edit(q,text,markup):
    try:
        await q.message.edit_text(text,parse_mode='HTML',reply_markup=markup)
    except TelegramBadRequest as exc:
        if 'message is not modified' in str(exc).lower():
            return
        raise

app=Dispatcher()
def allowed(user): return not ALLOWED_ID or ALLOWED_ID==user

BOT_VERSION = '3.3.2'

@app.message(Command('version'))
async def show_version(message: Message):
    await message.answer(f'🌱 Radmir Farm Bot v{BOT_VERSION}\nМеню цен: диагностическая сборка')

@app.message(Command('id'))
async def user_id(message: Message):
    await message.answer(f'Ваш Telegram ID: <code>{message.from_user.id}</code>',parse_mode='HTML')

@app.message(CommandStart())
@app.message(Command('menu'))
async def start(message: Message):
    u=message.from_user.id
    if not allowed(u): return await message.answer('⛔ Доступ закрыт')
    ensure(u)
    await message.answer(dashboard(u),parse_mode='HTML',reply_markup=home_kb(u))

@app.message()
async def numerical_input(message: Message):
    u=message.from_user.id
    if not allowed(u): return
    pending=db.execute('SELECT * FROM pending_inputs WHERE user_id=?',(u,)).fetchone()
    if not pending: return await message.answer('Открой меню командой /start')
    if pending['action']=='custom_name':
        name=(message.text or '').strip()
        if not (1<=len(name)<=45) or any(c in name for c in '<>&'):
            return await message.answer('Название: от 1 до 45 символов, без < и >.')
        with db: db.execute('INSERT INTO custom_stock_items(user_id,name) VALUES(?,?)',(u,name))
        state_clear(u)
        text,kb=stock_menu(u)
        return await message.answer('✅ Товар добавлен. Теперь настрой его цену через ⚙️ Настроить цены.\n\n'+text,parse_mode='HTML',reply_markup=kb)
    raw=(message.text or '').strip().replace(' ','').replace(',','.')
    try:
        value=float(raw)
        if not (0<value<=1000000000): raise ValueError
    except ValueError:
        return await message.answer('Введи положительное число, например 700 или 122500.')
    action=pending['action']
    if action in ('buy_qty','sale_qty'):
        if not value.is_integer() or value>1000000:
            return await message.answer('Количество должно быть целым числом от 1 до 1 000 000.')
        qty=int(value)
        if action=='buy_qty':
            sku=pending['sku']; price=unit_price(u,sku)
            if price is None:
                state_clear(u)
                return await message.answer('⚠️ Для этого материала ещё нет цены. Сначала укажи цену за штуку в ⚙️ Настройках цен.',reply_markup=keyboard([[('⚙️ Настроить цены','prices')]]))
            total=round(qty*price,2)
            with db:
                db.execute('INSERT INTO stock_moves(user_id,sku,quantity,paid,created_at,note) VALUES (?,?,?,?,?,?)',
                           (u,sku,qty,total,clock(),'Пополнение по сохранённой цене'))
            state_clear(u)
            return await message.answer(f'✅ <b>Склад пополнен!</b>\n{item_name(u,sku)}\n📦 +{qty} шт.\n💳 Цена за шт.: {money(price)}\n💸 Учтённые расходы: <b>{money(total)}</b>\n📦 Остаток: {units(u,sku)} шт.',parse_mode='HTML',reply_markup=keyboard([[('📦 Склад','stock'),('💰 Финансы','finance')]]))
        state_set(u,'sale_price','',qty)
        return await message.answer(f'💵 Продано: <b>{qty} перцев</b>\nВведи <b>общую выручку от продажи</b> в рублях.',parse_mode='HTML')
    if action=='set_unit_price':
        with db:
            db.execute('INSERT OR REPLACE INTO unit_prices(user_id,sku,price) VALUES(?,?,?)',(u,pending['sku'],value))
        state_clear(u)
        return await message.answer(f'✅ Цена сохранена: {item_name(u,pending["sku"])} — {money(value)} за 1 шт.',parse_mode='HTML',reply_markup=keyboard([[('⚙️ Цены','prices'),('📦 Склад','stock')]]))
    if action=='buy_price':
        with db:
            db.execute('INSERT INTO stock_moves(user_id,sku,quantity,paid,created_at,note) VALUES (?,?,?,?,?,?)',
                       (u,pending['sku'],pending['amount'],value,clock(),'Оптовая закупка'))
        state_clear(u)
        await message.answer(f'✅ <b>Закупка сохранена!</b>\n{item_name(u,pending["sku"])}\n'
                             f'📦 +{pending["amount"]} шт.\n💸 Потрачено: {money(value)}\n'
                             f'💳 Цена за шт.: {money(value/pending["amount"])}',
                             parse_mode='HTML',reply_markup=keyboard([[('📦 Склад','stock'),('💰 Финансы','finance')]]))
    elif action=='sale_price':
        with db:
            db.execute('INSERT INTO sales(user_id,quantity,total,created_at) VALUES(?,?,?,?)',
                       (u,pending['amount'],value,clock()))
        state_clear(u)
        await message.answer(f'✅ Продажа сохранена: {pending["amount"]} шт. за {money(value)}.',
                             reply_markup=keyboard([[('💰 Финансы','finance')]]))

@app.callback_query()
async def actions(q: CallbackQuery):
    u=q.from_user.id
    if not allowed(u): return await q.answer('Доступ закрыт',show_alert=True)
    ensure(u)
    args=(q.data or '').split(':'); key=args[0]
    try:
        if key in ('home','refresh','settings','finance','stock','prices'):
            state_clear(u)
        if key in ('home','refresh'):
            await edit(q,dashboard(u),home_kb(u))
        elif key=='settings':
            await edit(q,settings_view(u),keyboard([[('📦 Склад и цены','stock')],[('🏠 Главное меню','home')]]))
        elif key=='newgarden':
            gs=garden_nums(u)
            if len(gs)>=6: return await q.answer('Максимум 6 огородов',show_alert=True)
            g=next(i for i in range(1,7) if i not in gs)
            with db: db.execute('INSERT INTO gardens VALUES(?,?)',(u,g))
            await edit(q,f'✅ Добавлен огород №{g}!\n\n'+dashboard(u),home_kb(u))
        elif key=='g':
            g=int(args[1]); assert valid_garden(u,g)
            await edit(q,garden_summary(u,g),garden_kb(g))
        elif key=='templates':
            await edit(q,'⭐ <b>Шаблон посадки</b>\n19 перцев + 1 морковь.\nВыбери ПУСТОЙ огород. '
                         'Посадка создаст отдельные таймеры всех 20 кустов.',
                       keyboard([[(f'🌱 Огород №{g}',f'tplask:{g}')] for g in garden_nums(u)]+[[('🏠 Меню','home')]]))
        elif key=='tplask':
            g=int(args[1]); assert valid_garden(u,g)
            await edit(q,f'Подтверди, что ты <b>фактически посадил</b> 19 перцев и 1 морковь в огороде №{g}.',
                       keyboard([[('✅ Посадил',f'tpl:{g}'),('❌ Назад','templates')]]))
        elif key=='tpl':
            g=int(args[1]); assert valid_garden(u,g)
            if get_plants(u,g): return await q.answer('Шаблон только для пустого огорода',show_alert=True)
            with db:
                t=clock()
                db.executemany('INSERT INTO plants(user_id,garden,crop,planted_at,watered_at) VALUES(?,?,?,?,?)',
                               [(u,g,'pepper',t,t)]*19+[(u,g,'carrot',t,t)])
                record_plant_costs(u,['pepper']*19+['carrot'])
                db.execute('INSERT OR IGNORE INTO plant_extras(plant_id) SELECT id FROM plants WHERE user_id=? AND garden=? AND crop="pepper"',(u,g))
                db.execute('UPDATE plant_extras SET carrot_bonus=1 WHERE plant_id IN (SELECT id FROM plants WHERE user_id=? AND garden=? AND crop="pepper")',(u,g))
            mark_full_garden(u)
            await edit(q,'✅ 19 перцев и 1 морковь добавлены.\n\n'+garden_summary(u,g),garden_kb(g))
        elif key=='add':
            g=int(args[1]); assert valid_garden(u,g)
            rows=[[(label,f'qty:{g}:{crop}')] for crop,(label,_,_) in CROPS.items()]
            rows.append([('⭐ 19 перцев + 1 морковь',f'tplask:{g}')]); rows.append([('⬅️ Назад',f'g:{g}')])
            await edit(q,f'Свободно мест: {20-len(get_plants(u,g))}. Выбери культуру:',keyboard(rows))
        elif key=='qty':
            g=int(args[1]); c=args[2]; assert valid_garden(u,g) and c in CROPS
            free=20-len(get_plants(u,g))
            rows=[[(str(i),f'plant:{g}:{c}:{i}') for i in range(s,min(s+4,free+1))] for s in range(1,free+1,4)]
            rows.append([('⬅️ Назад',f'add:{g}')])
            await edit(q,f'{CROPS[c][0]}: сколько кустов ты уже посадил?',keyboard(rows))
        elif key=='plant':
            g=int(args[1]); c=args[2]; n=int(args[3])
            if not add_plants(u,g,c,n): return await q.answer('Нет места или неверное количество',show_alert=True)
            await edit(q,f'✅ Добавлено {n} кустов.\n\n'+garden_summary(u,g),garden_kb(g))
        elif key=='list':
            g=int(args[1]); assert valid_garden(u,g)
            text,kb=crop_list(u,g,int(args[2])); await edit(q,text,kb)
        elif key=='w':
            p=get_plant(u,int(args[1]))
            if not p: return await q.answer('Куст не найден',show_alert=True)
            do_water(u,[p]); text,kb=crop_list(u,p['garden'],0)
            await edit(q,'💧 Полив куста сохранён.\n\n'+text,kb)
        elif key=='wc':
            g=int(args[1]); assert valid_garden(u,g)
            groups=Counter(p['crop'] for p in get_plants(u,g))
            rows=[[(f'{CROPS[c][0]} ×{n}',f'askw:{g}:{c}')] for c,n in groups.items()]
            rows.append([('⬅️ Назад',f'g:{g}')])
            await edit(q,'Выбери культуру, которую ты уже полил:',keyboard(rows))
        elif key=='askw':
            g=int(args[1]); c=args[2]
            assert (g==0 or valid_garden(u,g)) and (c=='all' or c in CROPS)
            ps=get_plants(u,g if g else None)
            ps=[p for p in ps if c=='all' or p['crop']==c]
            if not ps: return await q.answer('Нет кустов',show_alert=True)
            title='ВСЕХ огородах' if g==0 else f'огороде №{g}'
            await edit(q,f'⚠️ Ты действительно <b>УЖЕ ПОЛИЛ</b> {len(ps)} кустов на {title}?\n'
                         'Подтверждение обновит индивидуальные таймеры этих кустов.',
                       keyboard([[('✅ Да, полил',f'dow:{g}:{c}'),('❌ Отмена','home' if g==0 else f'g:{g}')]]))
        elif key=='dow':
            g=int(args[1]); c=args[2]; assert (g==0 or valid_garden(u,g)) and (c=='all' or c in CROPS)
            ps=[p for p in get_plants(u,g if g else None) if c=='all' or p['crop']==c]
            n=do_water(u,ps)
            await edit(q,f'💧 Записан полив {n} кустов.\nМожно отменить последнее действие.',
                       keyboard([[('↩️ Отменить', 'undoask')],[('🏠 Меню','home')]]))
        elif key=='undoask':
            act=db.execute('SELECT id FROM water_actions WHERE user_id=? AND undone=0 ORDER BY id DESC LIMIT 1',(u,)).fetchone()
            if not act: return await q.answer('Нет полива для отмены',show_alert=True)
            await edit(q,'↩️ Вернуть время полива для кустов из последнего действия?\n'
                         'Это отменит только последнюю запись полива.',
                       keyboard([[('✅ Отменить полив','undodo'),('❌ Назад','home')]]))
        elif key=='undodo':
            n=undo_water(u)
            await edit(q,f'↩️ Отменён последний полив. Восстановлено кустов: {n}.\n\n'+dashboard(u),home_kb(u))
        elif key=='askclear':
            g=int(args[1]); assert valid_garden(u,g)
            await edit(q,f'🗑 Очистить огород №{g}?\nВсе его кусты и таймеры будут удалены. '
                         'Отменить очистку нельзя. Другие огороды не изменятся.',
                       keyboard([[('🗑 Да, очистить',f'clear:{g}'),('❌ Отмена',f'g:{g}')]]))
        elif key=='clear':
            g=int(args[1]); assert valid_garden(u,g)
            with db:
                db.execute('DELETE FROM plant_extras WHERE plant_id IN (SELECT id FROM plants WHERE user_id=? AND garden=?)',(u,g))
                db.execute('DELETE FROM plants WHERE user_id=? AND garden=?',(u,g))
            await edit(q,f'🗑 Огород №{g} очищен.\n'+garden_summary(u,g),garden_kb(g))
        elif key=='askdeletegarden':
            g=int(args[1]); assert valid_garden(u,g)
            if len(garden_nums(u)) <= 1:
                return await q.answer('Нельзя удалить единственный огород. Можно очистить его.',show_alert=True)
            n=len(get_plants(u,g))
            await edit(q,f'⚠️ <b>Удалить огород №{g}?</b>\n'
                         f'Все растения ({n} шт.) и таймеры этого огорода будут удалены.\n'
                         'История собранного урожая сохранится. Действие необратимо.',
                       keyboard([[('❌ Да, удалить',f'deletegarden:{g}')],
                                 [('↩️ Отмена',f'g:{g}')]]))
        elif key=='deletegarden':
            g=int(args[1]); assert valid_garden(u,g)
            if len(garden_nums(u)) <= 1:
                return await q.answer('Последний огород удалять нельзя',show_alert=True)
            with db:
                db.execute('DELETE FROM water_action_items WHERE plant_id IN '
                           '(SELECT id FROM plants WHERE user_id=? AND garden=?)',(u,g))
                db.execute('DELETE FROM plant_extras WHERE plant_id IN '
                           '(SELECT id FROM plants WHERE user_id=? AND garden=?)',(u,g))
                db.execute('DELETE FROM plants WHERE user_id=? AND garden=?',(u,g))
                db.execute('DELETE FROM gardens WHERE user_id=? AND num=?',(u,g))
            await edit(q,f'✅ Огород №{g} удалён.\n\n'+dashboard(u),home_kb(u))
        elif key=='askh':
            g=int(args[1]); assert valid_garden(u,g)
            ready=[p for p in get_plants(u,g) if is_ready(p)]
            if not ready: return await q.answer('Пока ничего не созрело',show_alert=True)
            await edit(q,f'Собрать {len(ready)} созревших кустов? Это удалит их из огорода.',
                       keyboard([[('✅ Собрал',f'doh:{g}'),('❌ Отмена',f'g:{g}')]]))
        elif key=='h':
            p=get_plant(u,int(args[1]))
            if not p: return await q.answer('Куст уже удалён',show_alert=True)
            if not is_ready(p): return await q.answer('Ещё не созрел',show_alert=True)
            with db:
                archive_harvest(u,[p])
                db.execute('DELETE FROM plant_extras WHERE plant_id=?',(p['id'],))
                db.execute('DELETE FROM plants WHERE id=? AND user_id=?',(p['id'],u))
            await edit(q,'✅ Куст собран.\n'+garden_summary(u,p['garden']),garden_kb(p['garden']))
        elif key=='doh':
            g=int(args[1]); assert valid_garden(u,g)
            ready=[p for p in get_plants(u,g) if is_ready(p)]
            ids=[p['id'] for p in ready]
            with db:
                archive_harvest(u,ready)
                db.executemany('DELETE FROM plant_extras WHERE plant_id=?',[(i,) for i in ids])
                db.executemany('DELETE FROM plants WHERE id=? AND user_id=?',[(i,u) for i in ids])
            await edit(q,f'✅ Собрано {len(ids)} кустов.\n'+garden_summary(u,g),garden_kb(g))
        elif key=='stockadd':
            state_set(u,'custom_name')
            await edit(q,'➕ <b>Новый товар</b>\nНапиши название товара одним сообщением (до 45 символов).',keyboard([[('❌ Отмена','stock')]]))
        elif key=='stockrestore':
            hidden=db.execute('SELECT sku FROM hidden_stock_items WHERE user_id=?',(u,)).fetchall()
            custom=db.execute('SELECT id,name FROM custom_stock_items WHERE user_id=? AND active=0',(u,)).fetchall()
            rows=[[(STOCK_ITEMS[r['sku']], f'stockrev:{r["sku"]}')] for r in hidden]
            rows += [[('📎 '+r['name'],f'stockrev:custom_{r["id"]}')] for r in custom]
            rows.append([('⬅️ Склад','stock')])
            await edit(q,'♻️ <b>Вернуть скрытый товар</b>\nВыбери позицию для восстановления.' if len(rows)>1 else 'Нет скрытых товаров.',keyboard(rows))
        elif key=='stockrev':
            sku=args[1]
            with db:
                if sku.startswith('custom_'):
                    db.execute('UPDATE custom_stock_items SET active=1 WHERE id=? AND user_id=?',(int(sku.split('_')[1]),u))
                else:
                    db.execute('DELETE FROM hidden_stock_items WHERE user_id=? AND sku=?',(u,sku))
            text,kb=stock_menu(u)
            await edit(q,'✅ Товар восстановлен.\n\n'+text,kb)
        elif key=='stockremove':
            items=[(name, f'stockdelask:{sku}') for sku,name in stock_items(u).items()]
            await edit(q,'🗑 <b>Убрать товар со склада</b>\nЭто скроет позицию из меню, но сохранит старые закупки.',keyboard([[x] for x in items]+[[('⬅️ Назад','stock')]]))
        elif key=='stockdelask':
            sku=args[1]; assert valid_item(u,sku)
            await edit(q,'⚠️ Убрать позицию «'+html.escape(item_name(u,sku))+'» из списка? История закупок останется.',keyboard([[('✅ Убрать',f'stockdel:{sku}')],[('❌ Отмена','stock')]]))
        elif key=='stockdel':
            sku=args[1]; assert valid_item(u,sku)
            if sku.startswith('custom_'):
                with db: db.execute('UPDATE custom_stock_items SET active=0 WHERE id=? AND user_id=?',(int(sku.split('_')[1]),u))
            else:
                # Built-in items cannot be removed from internal costs: hide them per user.
                with db: db.execute('INSERT OR IGNORE INTO hidden_stock_items(user_id,sku) VALUES(?,?)',(u,sku))
            text,kb=stock_menu(u)
            await edit(q,'✅ Товар скрыт.\n\n'+text,kb)
        elif key=='stock':
            text,kb=stock_menu(u); await edit(q,text,kb)
        elif key=='prices':
            logging.info('PRICE_MENU_OPEN version=%s user=%s data=%s', BOT_VERSION, u, q.data)
            text,kb=prices_menu(u); await edit(q,text,kb)
        elif key=='price':
            sku=args[1]; assert valid_item(u,sku)
            state_set(u,'set_unit_price',sku)
            await edit(q,f'⚙️ <b>Цена: {item_name(u,sku)}</b>\n\nТекущая цена: {money(unit_price(u,sku)) if unit_price(u,sku) is not None else "не задана"}\nНапиши цену <b>за 1 единицу</b> в рублях (например <code>262.5</code>).',keyboard([[('❌ Отмена','prices')]]))
        elif key=='buy':
            sku=args[1]; assert valid_item(u,sku)
            state_set(u,'buy_qty',sku)
            await edit(q,f'📦 <b>Закупка: {item_name(u,sku)}</b>\n\nНапиши сообщением количество единиц, например <code>700</code>.',
                       keyboard([[('❌ Отмена','stock')]]))
        elif key=='sale':
            state_set(u,'sale_qty')
            await edit(q,'💵 <b>Продажа перца</b>\nНапиши, сколько штук продал (например <code>171</code>).',
                       keyboard([[('❌ Отмена','finance')]]))
        elif key=='finance':
            await edit(q,finance_menu(u),keyboard([[('📦 Склад / Закупки','stock')], [('💵 Внести продажу перца','sale')], [('⬅️ Меню','home')]]))
        else:
            return await q.answer('Неизвестная кнопка',show_alert=True)
        await q.answer()
    except (ValueError, IndexError, AssertionError) as exc:
        logging.exception('Callback failed: key=%r data=%r user=%s', key, q.data, u)
        await q.answer('Ошибка обработки кнопки. Подробности в логах Bothost.', show_alert=True)

async def notifier(bot):
    while True:
        try:
            batches={}; t=clock()
            for p in db.execute('SELECT * FROM plants ORDER BY user_id,garden,id'):
                if not allowed(p['user_id']): continue
                if is_ready(p):
                    if not p['harvest_sent']:
                        batches.setdefault((p['user_id'],p['garden'],'harvest'),[]).append(p)
                    continue
                due=due_at(p)
                if t>=due and p['due_sent_for']!=p['watered_at']:
                    batches.setdefault((p['user_id'],p['garden'],'due'),[]).append(p)
                elif due-WARN<=t<due and p['warning_sent_for']!=p['watered_at']:
                    batches.setdefault((p['user_id'],p['garden'],'warn'),[]).append(p)
            for (u,g,kind),ps in batches.items():
                label={'warn':'⏰ СКОРО ПОЛИВ','due':'🚨 СРОЧНО: ПОЛИВ','harvest':'🎉 УРОЖАЙ ГОТОВ'}[kind]
                count=Counter(p['crop'] for p in ps)
                description=', '.join(f'{CROPS[c][0]} ×{n}' for c,n in count.items())
                try:
                    await bot.send_message(u,
                           f'🌱 <b>RADMiR FARM • ОГОРОД №{g}</b>\n'
                           f'━━━━━━━━━━━━━━━━━━\n<b>{label}</b>\n\n'
                           f'🪴 Растения: {description}\n'
                           f'🔢 Кустов: <b>{len(ps)}</b>\n'
                           + ('⏱ Осталось не более 3 минут.\n' if kind=='warn' else '')
                           + ('💧 Полей кусты в игре и подтверди действие ниже.\n' if kind=='due' else '')
                           + ('📦 Собери урожай в игре и отметь сбор.\n' if kind=='harvest' else '')
                           + '━━━━━━━━━━━━━━━━━━',
                           parse_mode='HTML',reply_markup=keyboard(
                             [[('💧 Полил огород',f'askw:{g}:all')] if kind!='harvest' else [('✅ Собрал',f'askh:{g}')],
                              [('🌱 Открыть огород',f'g:{g}')]]))
                except Exception:
                    logging.exception('Notification to user %s failed',u)
                    continue
                with db:
                    for p in ps:
                        if kind=='harvest':
                            db.execute('UPDATE plants SET harvest_sent=1 WHERE id=?',(p['id'],))
                        else:
                            column='warning_sent_for' if kind=='warn' else 'due_sent_for'
                            db.execute(f'UPDATE plants SET {column}=? WHERE id=? AND watered_at=?',
                                       (p['watered_at'],p['id'],p['watered_at']))
        except Exception:
            logging.exception('Background notifier failed')
        await asyncio.sleep(20)

async def main():
    if not TOKEN or TOKEN=='PASTE_YOUR_BOT_TOKEN_HERE':
        raise SystemExit('Configure BOT_TOKEN in Replit Secrets / .env')
    logging.basicConfig(level=logging.INFO)
    logging.warning('RADMIR_FARM_ACTIVE_VERSION=%s PID=%s', BOT_VERSION, os.getpid())
    bot=Bot(TOKEN)
    task=asyncio.create_task(notifier(bot))
    try:
        await app.start_polling(bot)
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        await bot.session.close()
        db.close()

if __name__=='__main__': asyncio.run(main())
