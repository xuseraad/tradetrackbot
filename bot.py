import os
import json
import base64
import logging
import anthropic
import gspread
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
SHEET_ID       = os.environ["GOOGLE_SHEET_ID"]
ALLOWED_USERS  = set(os.getenv("ALLOWED_USER_IDS", "").split(","))

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

EXTRACT_PROMPT = """Bu ekran görüntüsü bir kripto borsasındaki işlem detayı sayfasıdır.
Aşağıdaki bilgileri JSON formatında çıkar. Bulunamazsa null yaz.

Para birimi tespiti için:
- Fiyat/tutar yanında ₺ veya TL varsa para_birimi = "TL"
- Fiyat/tutar yanında USDT varsa para_birimi = "USDT"
- İşlem çiftinde /TL varsa para_birimi = "TL"
- İşlem çiftinde /USDT varsa para_birimi = "USDT"

Emir tipi için: "Limit Alış", "Limit Satış", "Market Alış", "Market Satış",
"Kolay Alış", "Kolay Satış" gibi değerleri olduğu gibi yaz.

{
  "platform": "borsa/uygulama adı",
  "islem_cifti": "örn: RAVE/USDT veya ENJ/TL",
  "token": "sadece token adı, örn: RAVE",
  "para_birimi": "USDT veya TL",
  "emir_tipi": "Limit Alış / Kolay Satış vb.",
  "durum": "Gerçekleşti / Beklemede / İptal",
  "emir_tarihi": "GG.AA.YYYY SS:DD:SS",
  "gerceklesme_tarihi": "GG.AA.YYYY SS:DD:SS",
  "girilen_adet": null,
  "limit_fiyat": null,
  "gerceklesen_miktar_token": sayı,
  "gerceklesen_fiyat": sayı,
  "gerceklesen_tutar": sayı,
  "komisyon": sayı,
  "toplam": sayı
}

Sadece JSON döndür, başka hiçbir şey yazma."""

# ─── Sabitler ────────────────────────────────────────────────────────────────
UNMATCHED   = "EŞLEŞMEDİ ⚠️"
STATUS_ACIK = "AÇIK"

KZ_HEADERS = [
    "TOKEN", "PARA BİRİMİ",
    "ALIŞ TARİHİ", "SATIŞ TARİHİ", "ALIŞ MİKTAR",
    "ALIŞ FİYAT", "SATIŞ FİYAT", "ALIŞ KOM", "SATIŞ KOM",
    "ALIŞ TUTAR", "SATIŞ TUTAR", "BRÜT K/Z", "K/Z %", "NET K/Z", "DURUM",
]
C = {h: i for i, h in enumerate(KZ_HEADERS)}

_gc = None

def sheets_client():
    global _gc
    if _gc is None:
        creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        _gc = gspread.authorize(creds)
    return _gc

# ─── Yardımcılar ─────────────────────────────────────────────────────────────
def _num(val) -> float:
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", ".").strip())
    except (ValueError, TypeError):
        return 0.0

def _safe(row, idx, default="?"):
    return row[idx] if len(row) > idx else default

def classify_order(emir_tipi: str) -> tuple[bool, bool]:
    et = (emir_tipi or "").lower()
    is_sell = any(k in et for k in ["satış", "satish", "sell"])
    is_buy  = not is_sell and any(k in et for k in ["alış", "alish", "buy"])
    return is_buy, is_sell

def get_currency_label(para_birimi: str) -> str:
    return "TL" if (para_birimi or "").upper() == "TL" else "USDT"

def split_datetime(raw: str) -> tuple[str, str]:
    if "," in raw:
        t, s = [x.strip() for x in raw.split(",", 1)]
        return t, s
    if " " in raw.strip():
        parts = raw.strip().split(" ", 1)
        return parts[0], parts[1]
    return raw, ""

def safe_komisyon(raw) -> float:
    try:
        return float(str(raw).replace(",", ".").strip()) if raw is not None else 0.0
    except (ValueError, TypeError):
        return 0.0

def _detect_image_type(data: bytes) -> str:
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if data[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    return "image/jpeg"

def extract_trade_data(image_bytes: bytes) -> dict:
    media_type = _detect_image_type(image_bytes)
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": EXTRACT_PROMPT},
            ],
        }],
    )
    raw = msg.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def get_or_create_islem_ws(sh):
    try:
        return sh.worksheet("İşlem Kayıtları")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("İşlem Kayıtları", rows=1000, cols=10)
        ws.append_row([
            "Gerçekleşme Tarihi", "Gerçekleşme Saati",
            "Emir Tipi", "Ürün",
            "Token Adet", "Fiyat Maliyet", "Maliyet Birim",
            "Komisyon", "Gerçekleşen Tutar", "Gerçekleşen Tutar Birim",
        ])
        return ws

def get_or_create_kz_ws(sh):
    try:
        return sh.worksheet("Kar-Zarar")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Kar-Zarar", rows=1000, cols=len(KZ_HEADERS))
        ws.append_row(KZ_HEADERS)
        return ws

# ─── Sadece Ham Veriyi Kaydetme ──────────────────────────────────────────────
def append_to_sheet(data: dict) -> tuple[bool, bool]:
    gc  = sheets_client()
    sh  = gc.open_by_key(SHEET_ID)

    para_birimi = get_currency_label(data.get("para_birimi", "USDT"))
    emir_tipi   = data.get("emir_tipi", "")
    token       = (data.get("token") or "").strip().upper()
    is_buy, is_sell = classify_order(emir_tipi)

    gerceklesme_raw = data.get("gerceklesme_tarihi") or data.get("emir_tarihi") or ""
    tarih_part, saat_part = split_datetime(gerceklesme_raw)
    komisyon_val = safe_komisyon(data.get("komisyon"))

    islem_ws = get_or_create_islem_ws(sh)

    # ── Mükerrer kontrol: aynı tarih+saat+token var mı? ─────────────────────
    existing = islem_ws.get_all_values()
    duplicate_row_index = None
    for i, row in enumerate(existing[1:], start=2):  # 1. satır başlık
        if len(row) >= 4 and row[0] == tarih_part and row[1] == saat_part and row[3].upper() == token:
            duplicate_row_index = i
            break

    islem_ws.append_row([
        tarih_part, saat_part,
        emir_tipi, token,
        data.get("gerceklesen_miktar_token") or "",
        data.get("gerceklesen_fiyat") or data.get("limit_fiyat") or "",
        para_birimi,
        komisyon_val,
        data.get("gerceklesen_tutar") or "",
        para_birimi,
    ], value_input_option="USER_ENTERED")

    return is_buy, is_sell, duplicate_row_index

# ─── Çekirdek Sıralama Motoru ────────────────────────────────────────────────
def jorik_sirala():
    gc = sheets_client()
    sh = gc.open_by_key(SHEET_ID)
    islem_ws = get_or_create_islem_ws(sh)
    
    all_rows = islem_ws.get_all_values()
    if len(all_rows) <= 1:
        return False
        
    header = all_rows[0]
    data_rows = all_rows[1:]
    
    def parse_row_date(row):
        try:
            dt_str = f"{row[0]} {row[1]}".strip()
            months = {"Ocak":"01","Şubat":"02","Mart":"03","Nisan":"04","Mayıs":"05","Haziran":"06","Temmuz":"07","Ağustos":"08","Eylül":"09","Ekim":"10","Kasım":"11","Aralık":"12"}
            for m_name, m_num in months.items():
                dt_str = dt_str.replace(m_name, m_num)
            return datetime.strptime(dt_str, "%d.%m.%Y %H:%M:%S")
        except Exception:
            return datetime.min

    data_rows.sort(key=parse_row_date)
    islem_ws.clear()
    islem_ws.append_rows([header] + data_rows, value_input_option="USER_ENTERED")
    return True

# ─── Çekirdek Eşleştirme Motoru (FIFO) ───────────────────────────────────────
def jorik_eslestir():
    gc = sheets_client()
    sh = gc.open_by_key(SHEET_ID)
    
    islem_ws = get_or_create_islem_ws(sh)
    kz_ws = get_or_create_kz_ws(sh)
    
    islem_rows = islem_ws.get_all_values()
    if len(islem_rows) <= 1:
        return False
        
    kz_ws.clear()
    kz_ws.append_row(KZ_HEADERS)
    
    pool = {}
    final_rows = []

    def calc_pnl(b_amt, b_fee, s_amt, s_fee):
        brut = round(s_amt - b_amt, 4)
        pct  = round((brut / b_amt * 100) if b_amt else 0.0, 2)
        net  = round(brut - b_fee - s_fee, 4)
        return brut, pct, net, ("KAR ✅" if net > 0 else "ZARAR ❌")

    for r in islem_rows[1:]:
        if len(r) < 10: continue
        tarih, saat, emir_tipi, token, miktar_raw, fiyat_raw, _, komisyon_raw, tutar_raw, pb = r
        token = token.strip().upper()
        pb = pb.strip().upper()
        
        qty = _num(miktar_raw)
        price = _num(fiyat_raw)
        fee = _num(komisyon_raw)
        amt = _num(tutar_raw)
        full_date = f"{tarih} {saat}".strip()
        
        is_buy, is_sell = classify_order(emir_tipi)
        pool_key = f"{token}_{pb}"
        
        if pool_key not in pool:
            pool[pool_key] = []
            
        if is_buy:
            pool[pool_key].append([full_date, price, qty, amt, fee])
        elif is_sell:
            rem_sell_qty = qty
            rem_sell_amt = amt
            
            while rem_sell_qty > 0.000001 and pool[pool_key]:
                b_date, b_price, b_qty, b_amt, b_fee = pool[pool_key][0]
                matched_qty = min(rem_sell_qty, b_qty)
                
                b_ratio = matched_qty / b_qty
                s_ratio = matched_qty / qty
                
                u_b_amt = round(b_amt * b_ratio, 4)
                u_b_fee = round(b_fee * b_ratio, 4)
                u_s_amt = round(amt * s_ratio, 4)
                u_s_fee = round(fee * s_ratio, 4)
                
                brut, pct, net, status = calc_pnl(u_b_amt, u_b_fee, u_s_amt, u_s_fee)
                final_rows.append([
                    token, pb, b_date, full_date, matched_qty,
                    b_price, price, u_b_fee, u_s_fee, u_b_amt, u_s_amt,
                    brut, pct, net, status
                ])
                
                b_qty_left = round(b_qty - matched_qty, 8)
                if b_qty_left > 0.000001:
                    pool[pool_key][0] = [
                        b_date, b_price, b_qty_left,
                        round(b_amt - u_b_amt, 4),
                        round(b_fee - u_b_fee, 4)
                    ]
                else:
                    pool[pool_key].pop(0)
                    
                rem_sell_qty -= matched_qty
                rem_sell_amt -= u_s_amt
                
            if rem_sell_qty > 0.000001:
                s_ratio = rem_sell_qty / qty
                final_rows.append([
                    token, pb, "", full_date, rem_sell_qty,
                    "", price, "", round(fee * s_ratio, 4), "", round(amt * s_ratio, 4),
                    "", "", "", UNMATCHED
                ])

    for pool_key, open_buys in pool.items():
        token, pb = pool_key.split("_")
        for b_date, b_price, b_qty, b_amt, b_fee in open_buys:
            final_rows.append([
                token, pb, b_date, "", b_qty,
                b_price, "", b_fee, "", b_amt,
                "", "", "", "", STATUS_ACIK
            ])
            
    if final_rows:
        kz_ws.append_rows(final_rows, value_input_option="USER_ENTERED")
    return True

# ─── Telegram Komut Arayüzleri ───────────────────────────────────────────────
def _auth(update) -> bool:
    user_id = str(update.effective_user.id)
    return ALLOWED_USERS == {""} or user_id in ALLOWED_USERS

async def cmd_sirala(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    await update.message.reply_text("⏳ İşlem Kayıtları tarih sırasına diziliyor...")
    if jorik_sirala():
        await update.message.reply_text("✅ İlk sekme başarıyla kronolojik sıraya dizildi!")
    else:
        await update.message.reply_text("📭 Sıralanacak işlem bulunamadı.")

async def cmd_eslestir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    await update.message.reply_text("🔄 Kar-Zarar tablosu FIFO mantığıyla sıfırdan hesaplanıyor...")
    if jorik_eslestir():
        await update.message.reply_text("🎯 Kar-Zarar tablosu başarıyla güncellendi!")
    else:
        await update.message.reply_text("📭 İşlenecek veri bulunamadı.")

# ─── Gecikmeli Otomatik Tetikleme Havuzu (Debounce) ───────────────────────────
# Her chat için aktif bir zamanlayıcı görevini ve sayaç durumunu tutar
_user_tasks = {}
_pending_counts = {}

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USERS != {""} and user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Erişim izniniz yok.")
        return

    # Bekleyen işlem sayısını artır
    _pending_counts[user_id] = _pending_counts.get(user_id, 0) + 1
    
    photo     = await update.message.photo[-1].get_file()
    img_bytes = await photo.download_as_bytearray()

    try:
        data = extract_trade_data(bytes(img_bytes))
        is_buy, is_sell, duplicate_row = append_to_sheet(data)
        log.info(f"Fotoğraf ham veri olarak eklendi. Chat: {user_id}")
    except Exception as e:
        log.exception("Görsel işleme hatası")
        await update.message.reply_text(f"⚠️ Bir görsel okunamadı veya kaydedilemedi: {e}")
        _pending_counts[user_id] = max(0, _pending_counts.get(user_id, 1) - 1)
        if user_id in _user_tasks:
            _user_tasks[user_id].cancel()
            _user_tasks.pop(user_id, None)
        return

    # ── Mükerrer tespit: kullanıcıya sor ─────────────────────────────────────
    if duplicate_row is not None:
        token = (data.get("token") or "?").upper()
        tarih = data.get("gerceklesme_tarihi") or data.get("emir_tarihi") or "?"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Evet, sil", callback_data=f"dup_sil:{duplicate_row}"),
            InlineKeyboardButton("❌ Hayır, kalsın", callback_data="dup_kalsın"),
        ]])
        await update.message.reply_text(
            f"⚠️ *Mükerrer kayıt tespit edildi!*\n\n"
            f"🪙 Token: `{token}`\n"
            f"📅 Tarih: `{tarih}`\n\n"
            f"Bu işlem daha önce kaydedilmiş görünüyor. "
            f"Eski kaydı silmek ister misiniz?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        # Debounce sayacını geri al (mükerrer onaylanana kadar sıralama yapma)
        _pending_counts[user_id] = max(0, _pending_counts.get(user_id, 1) - 1)
        return

    # Eğer bu kullanıcı için zaten çalışan bir geri sayım varsa iptal et (Süreyi uzatıyoruz)
    if user_id in _user_tasks:
        _user_tasks[user_id].cancel()

    # Yeni bir geri sayım görevi başlat (5 saniye hareketsizlik bekle)
    _user_tasks[user_id] = asyncio.create_task(trigger_delayed_sync(update, ctx, user_id))

async def trigger_delayed_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_id: str):
    try:
        await asyncio.sleep(5.0)  # Fotoğraflar arası maksimum bekleme penceresi
        
        count = _pending_counts.get(user_id, 1)
        await update.message.reply_text(f"şimdi {count} adet fotoğraf algılandı, arka planda otomatik sıralanıp eşleştiriliyor... ⏳")
        
        # Sırasıyla çekirdek motorları çalıştır
        jorik_sirala()
        jorik_eslestir()
        
        await update.message.reply_text("✅ Gönderdiğiniz tüm fotoğraflar başarıyla kronolojik sıraya dizildi ve Kar-Zarar tablonuz sıfırdan kusursuzca eşleştirildi! 🎯")
        
    except asyncio.CancelledError:
        # Görev iptal edildiyse sorun yok, yeni fotoğraf süreyi sıfırladı demektir
        pass
    finally:
        # Süreç bittiyse havuz kayıtlarını temizle
        if user_id in _user_tasks and _user_tasks[user_id] == asyncio.current_task():
            _user_tasks.pop(user_id, None)
            _pending_counts.pop(user_id, None)

# ─── Diğer Standart Komutlar ve Yapı ─────────────────────────────────────────
async def cmd_acik(update, ctx):
    if not _auth(update): return
    try:
        sh = sheets_client().open_by_key(SHEET_ID)
        ws = sh.worksheet("Kar-Zarar")
        rows = ws.get_all_values()
        if len(rows) < 2:
            await update.message.reply_text("📭 Henüz kayıt yok.")
            return
        acik = [r for r in rows[1:] if len(r) > C["DURUM"] and r[C["DURUM"]] == STATUS_ACIK]
        if not acik:
            await update.message.reply_text("✅ Açık pozisyon yok.")
            return
        lines = ["🗂 *Açık Pozisyonlar*\n"]
        for r in acik:
            pb, cur = _safe(r, C["PARA BİRİMİ"]), "₺" if _safe(r, C["PARA BİRİMİ"]) == "TL" else "$"
            lines.append(f"🪙 *{_safe(r, C['TOKEN'])}* ({pb})\n   Alış: `{cur}{_safe(r, C['ALIŞ FİYAT'])}` × `{_safe(r, C['ALIŞ MİKTAR'])}` \n   Tutar: `{cur}{_safe(r, C['ALIŞ TUTAR'])}`\n")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f"⚠️ Hata: {e}")

def get_pnl_rows(para_birimi_filter):
    try: return [r for r in sheets_client().open_by_key(SHEET_ID).worksheet("Kar-Zarar").get_all_values()[1:] if len(r) > C["PARA BİRİMİ"] and r[C["PARA BİRİMİ"]] == para_birimi_filter]
    except: return []

async def cmd_ozet(update, ctx, para_birimi):
    if not _auth(update): return
    cur = "₺" if para_birimi == "TL" else "$"
    try:
        rows = get_pnl_rows(para_birimi)
        if not rows:
            await update.message.reply_text(f"📭 {para_birimi} işlemi bulunamadı.")
            return
        kapali = [r for r in rows if len(r) > C["DURUM"] and r[C["DURUM"]] not in (STATUS_ACIK, UNMATCHED)]
        acik = [r for r in rows if len(r) > C["DURUM"] and r[C["DURUM"]] == STATUS_ACIK]
        toplam_net = sum(_num(r[C["NET K/Z"]]) for r in kapali)
        toplam_brut = sum(_num(r[C["BRÜT K/Z"]]) for r in kapali)
        lines = [f"📈 *{para_birimi} Özeti*\n", f"💰 Net K/Z: `{cur}{round(toplam_net, 2)}`", f"📊 Brüt K/Z: `{cur}{round(toplam_brut, 2)}` \n", f"✅ Kârlı işlem: `{sum(1 for r in kapali if 'KAR' in r[C['DURUM']])}`", f"❌ Zararlı işlem: `{sum(1 for r in kapali if 'ZARAR' in r[C['DURUM']])}`", f"🗂 Açık pozisyon: `{len(acik)}`", f"💼 Açık tutar: `{cur}{round(sum(_num(r[C['ALIŞ TUTAR']]) for r in acik), 2)}`"]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f"⚠️ Hata: {e}")

async def cmd_ozet_usdt(update, ctx): await cmd_ozet(update, ctx, "USDT")
async def cmd_ozet_tl(update, ctx): await cmd_ozet(update, ctx, "TL")

async def cmd_token(update, ctx):
    if not _auth(update): return
    if not ctx.args: return await update.message.reply_text("Kullanım: /token ELIZAOS")
    aranan = ctx.args[0].upper()
    try:
        rows = sheets_client().open_by_key(SHEET_ID).worksheet("Kar-Zarar").get_all_values()
        eslesen = [r for r in rows[1:] if len(r) > C["TOKEN"] and r[C["TOKEN"]].upper() == aranan]
        if not eslesen: return await update.message.reply_text(f"❌ `{aranan}` için kayıt bulunamadı.", parse_mode="Markdown")
        lines = [f"🔍 *{aranan} Geçmişi*\n"]
        for r in eslesen:
            pb, cur, durum = _safe(r, C["PARA BİRİMİ"]), "₺" if _safe(r, C["PARA BİRİMİ"]) == "TL" else "$", _safe(r, C["DURUM"])
            if durum == STATUS_ACIK: lines.append(f"🗂 *AÇIK* | {pb}\n   Alış: `{_safe(r, C['ALIŞ TARİHİ'])}` @ `{cur}{_safe(r, C['ALIŞ FİYAT'])}` × `{_safe(r, C['ALIŞ MİKTAR'])}` \n")
            elif durum == UNMATCHED: lines.append(f"⚠️ EŞLEŞMEDİ | {pb}\n   Satış: `{_safe(r, C['SATIŞ TARİHİ'])}` × `{_safe(r, C['ALIŞ MİKTAR'])}` \n")
            else: lines.append(f"{durum} | {pb}\n   Alış: `{_safe(r, C['ALIŞ TARİHİ'])}` @ `{cur}{_safe(r, C['ALIŞ FİYAT'])}` \n   Satış: `{_safe(r, C['SATIŞ TARİHİ'])}` \n   Net K/Z: `{cur}{_safe(r, C['NET K/Z'])}` (`%{_safe(r, C['K/Z %'])}`)\n")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f"⚠️ Hata: {e}")

async def cmd_yardim(update, ctx):
    await update.message.reply_text(
        "🤖 *Kripto İşlem Kayıt Botu (Akıllı Otomatik FIFO Modu)*\n\n"
        "📸 *Nasıl kullanılır?*\n"
        "Fotoğrafları ister tek tek, ister *toplu albüm* halinde gönderin. Bot hepsini hafızaya alır, fotoğraf akışınız durduktan 5 saniye sonra otomatik sıralar ve tüm havuzunuzu hatasızca baştan aşağı eşleştirir.\n\n"
        "📊 *Sorgulama ve Manuel Yönetim*\n"
        "/sirala — Manuel kronolojik sıralama\n"
        "/eslestir — Manuel FIFO hesaplama motoru\n"
        "/acik — Tüm açık pozisyonlar\n"
        "/ozet_usdt — USDT K/Z özeti\n"
        "/ozet_tl — TL K/Z özeti\n"
        "/token [AD] — Özel token geçmişi\n"
        "/sil — Son kaydı temizler", parse_mode="Markdown"
    )

async def cmd_sil(update, ctx):
    if not _auth(update): return
    try:
        sh = sheets_client().open_by_key(SHEET_ID)
        for sekme, token_col in [("İşlem Kayıtları", 3), ("Kar-Zarar", C["TOKEN"])]:
            ws = sh.worksheet(sekme)
            rows = ws.get_all_values()
            if len(rows) > 1: ws.delete_rows(len(rows))
        await update.message.reply_text("✅ Son eklenen ham kayıt ve Kar-Zarar satırı silindi. Doğru sıraya oturması için komutları elinizle tetikleyebilirsiniz.")
    except Exception as e: await update.message.reply_text(f"⚠️ Hata: {e}")

async def handle_duplicate_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("dup_sil:"):
        row_index = int(query.data.split(":")[1])
        try:
            sh = sheets_client().open_by_key(SHEET_ID)
            islem_ws = sh.worksheet("İşlem Kayıtları")
            islem_ws.delete_rows(row_index)
            await query.edit_message_text(
                "✅ Eski mükerrer kayıt silindi. Yeni kayıt geçerli olarak tutuldu."
            )
            log.info(f"Mükerrer satır {row_index} silindi.")
        except Exception as e:
            await query.edit_message_text(f"⚠️ Silme işlemi başarısız: {e}")
    else:
        await query.edit_message_text(
            "ℹ️ Her iki kayıt da tutuldu. İstediğinizde /sil komutuyla son kaydı kaldırabilirsiniz."
        )

async def handle_text(update, ctx):
    await update.message.reply_text("👋 Ekran görüntülerinizi toplu veya tek tek gönderebilirsiniz. Sistem otomatik işleyip havuzu güncelleyecektir. Komut listesi için /yardim yazabilirsiniz.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("sirala",     cmd_sirala))
    app.add_handler(CommandHandler("eslestir",   cmd_eslestir))
    app.add_handler(CommandHandler("acik",      cmd_acik))
    app.add_handler(CommandHandler("ozet_usdt", cmd_ozet_usdt))
    app.add_handler(CommandHandler("ozet_tl",   cmd_ozet_tl))
    app.add_handler(CommandHandler("token",     cmd_token))
    app.add_handler(CommandHandler("sil",       cmd_sil))
    app.add_handler(CommandHandler("yardim",    cmd_yardim))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_duplicate_callback, pattern="^dup_"))
    log.info("Bot akıllı dinamik modda başlatıldı...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()