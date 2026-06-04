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

EXTRACT_SYSTEM = """Sen bir kripto borsa ekran görüntüsü analiz uzmanısın. 
Görevin: işlem detayı içeren ekran görüntülerinden verileri doğru ve eksiksiz çıkarmak.
- Görüntü sıkıştırılmış veya düşük çözünürlüklü olsa bile tüm görünür metni dikkatlice oku.
- Rakamları, tarihleri ve token isimlerini olduğu gibi al, yorumlama.
- Sadece geçerli JSON döndür, markdown veya açıklama ekleme."""

EXTRACT_PROMPT = """Bu ekran görüntüsü bir kripto borsasındaki işlem detayı sayfasıdır.
Aşağıdaki tüm alanları eksiksiz JSON formatında çıkar. Bulunamazsa null yaz.

Para birimi tespiti:
- ₺ veya TL görünüyorsa → "TL"
- USDT görünüyorsa → "USDT"
- /TL çifti varsa → "TL", /USDT varsa → "USDT"

Emir tipi: Ekranda tam olarak ne yazıyorsa yaz ("Limit Alış", "Market Satış", "Kolay Alış" vb.)

Tarih formatı: Ekranda ne yazıyorsa yaz, sonra GG.AA.YYYY SS:DD:SS olarak normalize et.

Yanıt formatı — sadece bu JSON, başka hiçbir şey:
{
  "platform": null,
  "islem_cifti": null,
  "token": null,
  "para_birimi": null,
  "emir_tipi": null,
  "durum": null,
  "emir_tarihi": null,
  "gerceklesme_tarihi": null,
  "girilen_adet": null,
  "limit_fiyat": null,
  "gerceklesen_miktar_token": null,
  "gerceklesen_fiyat": null,
  "gerceklesen_tutar": null,
  "komisyon": null,
  "toplam": null
}"""

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

def sheets_client():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

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

    def _call():
        return claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=EXTRACT_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": EXTRACT_PROMPT},
                ],
            }],
        )

    for attempt in range(3):
        msg = _call()
        raw = msg.content[0].text.strip()
        # JSON bloğunu temizle
        raw = raw.replace("```json", "").replace("```", "").strip()
        # Bazen önünde/arkasında metin gelirse sadece { } bloğunu al
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start != -1 and end > start:
            raw = raw[start:end]
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning(f"JSON parse hatası (deneme {attempt+1}/3): {e}\nHam: {raw[:200]}")
            if attempt == 2:
                raise ValueError(f"Claude 3 denemede geçerli JSON döndürmedi: {e}") from e

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

    return is_buy, is_sell

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
        token, pb = pool_key.rsplit("_", 1)
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
        append_to_sheet(data)
        log.info(f"Fotoğraf ham veri olarak eklendi. Chat: {user_id}")
    except Exception as e:
        log.exception("Görsel işleme hatası")
        await update.message.reply_text(f"⚠️ Bir görsel okunamadı veya kaydedilemedi: {e}")
        _pending_counts[user_id] = max(0, _pending_counts.get(user_id, 1) - 1)
        if user_id in _user_tasks:
            _user_tasks[user_id].cancel()
            _user_tasks.pop(user_id, None)
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
        _pending_counts.pop(user_id, None)
        
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

async def cmd_ozet(update, ctx):
    if not _auth(update): return
    try:
        sh = sheets_client().open_by_key(SHEET_ID)
        rows = sh.worksheet("Kar-Zarar").get_all_values()
        if len(rows) < 2:
            await update.message.reply_text("📭 Henüz kayıt yok.")
            return

        lines = []
        for para_birimi, cur in [("TL", "₺"), ("USDT", "$")]:
            pb_rows = [r for r in rows[1:] if len(r) > C["PARA BİRİMİ"] and r[C["PARA BİRİMİ"]] == para_birimi]
            if not pb_rows:
                continue
            kapali = [r for r in pb_rows if len(r) > C["DURUM"] and r[C["DURUM"]] not in (STATUS_ACIK, UNMATCHED)]
            acik   = [r for r in pb_rows if len(r) > C["DURUM"] and r[C["DURUM"]] == STATUS_ACIK]
            toplam_net  = sum(_num(r[C["NET K/Z"]])    for r in kapali)
            toplam_brut = sum(_num(r[C["BRÜT K/Z"]])   for r in kapali)
            acik_tutar  = sum(_num(r[C["ALIŞ TUTAR"]]) for r in acik)
            karli   = sum(1 for r in kapali if "KAR"   in r[C["DURUM"]])
            zarari  = sum(1 for r in kapali if "ZARAR" in r[C["DURUM"]])
            lines += [
                f"━━━━━━━━━━━━━━━━",
                f"📈 *{para_birimi} Özeti*",
                f"💰 Net K/Z: `{cur}{round(toplam_net, 2)}`",
                f"📊 Brüt K/Z: `{cur}{round(toplam_brut, 2)}`",
                f"✅ Kârlı: `{karli}`  ❌ Zararlı: `{zarari}`",
                f"🗂 Açık pozisyon: `{len(acik)}`",
                f"💼 Açık tutar: `{cur}{round(acik_tutar, 2)}`",
            ]

        if not lines:
            await update.message.reply_text("📭 Henüz işlem kaydı yok.")
            return

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Hata: {e}")

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
        "/ozet — TL ve USDT K/Z özeti\n"
        "/token [AD] — Özel token geçmişi\n"
        "/sil — Son kaydı siler (onay ister)\n"
        "/mukerrer — Mükerrer kayıtları tara ve sil", parse_mode="Markdown"
    )

async def cmd_sil(update, ctx):
    if not _auth(update): return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Evet, sil", callback_data="sil_onayla"),
        InlineKeyboardButton("❌ İptal",     callback_data="sil_iptal"),
    ]])
    await update.message.reply_text(
        "⚠️ *Son kaydı silmek istediğinizden emin misiniz?*\n\n"
        "İşlem Kayıtları ve Kar-Zarar sekmelerindeki son satır silinecek.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

async def cmd_mukerrer(update, ctx):
    if not _auth(update): return
    try:
        sh = sheets_client().open_by_key(SHEET_ID)
        ws = sh.worksheet("İşlem Kayıtları")
        rows = ws.get_all_values()
        if len(rows) < 2:
            await update.message.reply_text("📭 Henüz kayıt yok.")
            return

        # tarih+saat+token kombinasyonlarını tara
        seen = {}       # (tarih, saat, token) -> ilk satır no
        duplicates = [] # (yeni_satır_no, tarih, saat, token)
        for i, row in enumerate(rows[1:], start=2):
            if len(row) < 4: continue
            key = (row[0].strip(), row[1].strip(), row[3].strip().upper())
            if key in seen:
                duplicates.append((i, key[0], key[1], key[2], seen[key]))
            else:
                seen[key] = i

        if not duplicates:
            await update.message.reply_text("✅ Mükerrer kayıt bulunamadı.")
            return

        for (yeni, tarih, saat, token, eski) in duplicates:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Eskiyi sil",  callback_data=f"dup_sil:{eski}"),
                InlineKeyboardButton("🗑 Yeniyi sil",  callback_data=f"dup_sil:{yeni}"),
                InlineKeyboardButton("❌ Bırak",       callback_data="dup_kalsın"),
            ]])
            await update.message.reply_text(
                f"⚠️ *Mükerrer kayıt bulundu!*\n\n"
                f"🪙 Token: `{token}`\n"
                f"📅 Tarih: `{tarih}` — `{saat}`\n\n"
                f"Satır {eski} (eski) ve Satır {yeni} (yeni) aynı.\n"
                f"Hangisini silmek istersiniz?",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Hata: {e}")

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── /sil onayı ───────────────────────────────────────────────────────────
    if data == "sil_onayla":
        try:
            sh = sheets_client().open_by_key(SHEET_ID)
            for sekme in ["İşlem Kayıtları", "Kar-Zarar"]:
                ws = sh.worksheet(sekme)
                rows = ws.get_all_values()
                if len(rows) > 1:
                    ws.delete_rows(len(rows))
            await query.edit_message_text("✅ Son kayıt silindi.")
            log.info("Son satır kullanıcı onayıyla silindi.")
        except Exception as e:
            await query.edit_message_text(f"⚠️ Silme başarısız: {e}")

    elif data == "sil_iptal":
        await query.edit_message_text("↩️ Silme işlemi iptal edildi.")

    # ── /mukerrer işlemleri ──────────────────────────────────────────────────
    elif data.startswith("dup_sil:"):
        row_index = int(data.split(":")[1])
        try:
            sh = sheets_client().open_by_key(SHEET_ID)
            ws = sh.worksheet("İşlem Kayıtları")
            ws.delete_rows(row_index)
            await query.edit_message_text(f"✅ Satır {row_index} silindi.")
            log.info(f"Mükerrer satır {row_index} silindi.")
        except Exception as e:
            await query.edit_message_text(f"⚠️ Silme başarısız: {e}")

    elif data == "dup_kalsın":
        await query.edit_message_text("ℹ️ Kayıt olduğu gibi bırakıldı.")

async def handle_text(update, ctx):
    await update.message.reply_text("👋 Ekran görüntülerinizi toplu veya tek tek gönderebilirsiniz. Sistem otomatik işleyip havuzu güncelleyecektir. Komut listesi için /yardim yazabilirsiniz.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("sirala",    cmd_sirala))
    app.add_handler(CommandHandler("eslestir",  cmd_eslestir))
    app.add_handler(CommandHandler("acik",      cmd_acik))
    app.add_handler(CommandHandler("ozet",      cmd_ozet))
    app.add_handler(CommandHandler("token",     cmd_token))
    app.add_handler(CommandHandler("sil",       cmd_sil))
    app.add_handler(CommandHandler("mukerrer",  cmd_mukerrer))
    app.add_handler(CommandHandler("yardim",    cmd_yardim))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    log.info("Bot akıllı dinamik modda başlatıldı...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()