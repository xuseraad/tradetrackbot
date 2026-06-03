import os
import json
import base64
import logging
import anthropic
import gspread
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
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
  "girilen_adet": sayı veya null,
  "limit_fiyat": sayı veya null,
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

# Sütun sırası — görseldeki düzene göre
KZ_HEADERS = [
    "TOKEN", "PARA BİRİMİ",
    "ALIŞ TARİHİ", "SATIŞ TARİHİ", "ALIŞ MİKTAR",
    "ALIŞ FİYAT", "SATIŞ FİYAT", "ALIŞ KOM", "SATIŞ KOM",
    "ALIŞ TUTAR", "SATIŞ TUTAR", "BRÜT K/Z", "K/Z %", "NET K/Z", "DURUM",
]
C = {h: i for i, h in enumerate(KZ_HEADERS)}

# ─── Google Sheets client (önbellekli) ───────────────────────────────────────
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
    """'6 Mart 2026, 12:55:06' → ('6 Mart 2026', '12:55:06')"""
    if "," in raw:
        t, s = [x.strip() for x in raw.split(",", 1)]
        return t, s
    return raw, ""


def safe_komisyon(raw) -> float:
    try:
        return float(str(raw).replace(",", ".").strip()) if raw is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


# ─── Claude vision ───────────────────────────────────────────────────────────
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


# ─── Sheet yardımcıları ───────────────────────────────────────────────────────
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


# ─── ANA FONKSİYON: append_to_sheet (pozisyon havuzu mantığı) ─────────────────
#
# Kar-Zarar sekmesindeki mantık:
#   - Her AÇIK satır bir alış partisini temsil eder (birden fazla olabilir)
#   - Satış geldiğinde: FIFO sırasıyla AÇIK satırları tüketir
#       • Satış miktarı = açık alış miktarı → satır kapatılır (KAR/ZARAR)
#       • Satış miktarı < açık alış miktarı → alış satırı güncellenir (kalan miktar/tutar),
#         ayrı bir "kapalı" satır eklenir
#       • Satış miktarı > tek alış → birden fazla alış satırı tüketilir
#   - Alış geldiğinde: UNMATCHED bekleyen satış varsa önce onunla eşleştir,
#     yoksa yeni AÇIK satır ekle
#
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

    # ── İşlem Kayıtları ──────────────────────────────────────────────────────
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

    # ── Kar-Zarar (pozisyon havuzu) ───────────────────────────────────────────
    # Kural: Her işlem tek satırda temsil edilir. Kısmi satışta:
    #   - Mevcut AÇIK satırın miktarı/tutarı/komisyonu güncellenir (azaltılır)
    #   - Satışa ait K/Z bilgileri ayrı yeni satır olarak eklenir
    # Böylece sheet'te hiçbir zaman "boş" ara satır oluşmaz.
    kz_ws    = get_or_create_kz_ws(sh)
    all_rows = kz_ws.get_all_values()

    if not token:
        return is_buy, is_sell

    def row_matches(r, status):
        return (
            len(r) > C["DURUM"] and
            r[C["TOKEN"]].upper() == token and
            r[C["PARA BİRİMİ"]]   == para_birimi and
            r[C["DURUM"]]         == status
        )

    def col_letter(idx):
        """0-tabanlı sütun index → A, B, ... Z, AA, ..."""
        result = ""
        idx += 1
        while idx:
            idx, rem = divmod(idx - 1, 26)
            result = chr(65 + rem) + result
        return result

    LAST_COL = col_letter(len(KZ_HEADERS) - 1)  # "O" (15 sütun)

    def calc_pnl(buy_amt, buy_fee, sell_amt, sell_fee):
        brut = round(sell_amt - buy_amt, 4)
        pct  = round((brut / buy_amt * 100) if buy_amt else 0.0, 2)
        net  = round(brut - buy_fee - sell_fee, 4)
        return brut, pct, net, ("KAR ✅" if net > 0 else "ZARAR ❌")

    # ── ALIŞ ─────────────────────────────────────────────────────────────────
    if is_buy:
        buy_qty    = _num(data.get("gerceklesen_miktar_token"))
        buy_amount = _num(data.get("gerceklesen_tutar"))
        buy_price  = _num(data.get("gerceklesen_fiyat") or data.get("limit_fiyat"))
        buy_fee    = komisyon_val

        # Bekleyen satış var mı?
        pending = next(
            ((i + 1, r) for i, r in enumerate(all_rows[1:], start=1) if row_matches(r, UNMATCHED)),
            None
        )

        if pending:
            ps_idx, ps_row = pending
            sheet_row  = ps_idx + 1
            sell_qty   = _num(ps_row[C["ALIŞ MİKTAR"]])   # UNMATCHED satırda miktar ALIŞ MİKTAR'da saklanır
            sell_amt   = _num(ps_row[C["ALIŞ TUTAR"]])
            sell_fee   = _num(ps_row[C["ALIŞ KOM"]])
            sell_price = _num(ps_row[C["ALIŞ FİYAT"]])
            sell_date  = ps_row[C["ALIŞ TARİHİ"]]

            matched   = min(buy_qty, sell_qty)
            b_ratio   = matched / buy_qty  if buy_qty  > 0 else 1.0
            s_ratio   = matched / sell_qty if sell_qty > 0 else 1.0
            used_b_amt = round(buy_amount * b_ratio, 4)
            used_b_fee = round(buy_fee    * b_ratio, 4)
            used_s_amt = round(sell_amt   * s_ratio, 4)
            used_s_fee = round(sell_fee   * s_ratio, 4)
            brut, pct, net, status = calc_pnl(used_b_amt, used_b_fee, used_s_amt, used_s_fee)

            # Mevcut UNMATCHED satırı → kapalı K/Z satırına dönüştür
            kz_ws.update(
                f"A{sheet_row}:{LAST_COL}{sheet_row}",
                [[
                    token, para_birimi,
                    gerceklesme_raw,          # ALIŞ TARİHİ
                    sell_date,                # SATIŞ TARİHİ
                    matched,                  # ALIŞ MİKTAR
                    buy_price,                # ALIŞ FİYAT
                    sell_price,               # SATIŞ FİYAT
                    used_b_fee,               # ALIŞ KOM
                    used_s_fee,               # SATIŞ KOM
                    used_b_amt,               # ALIŞ TUTAR
                    used_s_amt,               # SATIŞ TUTAR
                    brut, pct, net, status,
                ]],
                value_input_option="USER_ENTERED",
            )

            # Alıştan kalan → yeni AÇIK
            leftover_qty = round(buy_qty - matched, 8)
            if leftover_qty > 0.000001:
                lb_amt = round(buy_amount - used_b_amt, 4)
                lb_fee = round(buy_fee    - used_b_fee, 4)
                kz_ws.append_row([
                    token, para_birimi,
                    gerceklesme_raw, "",       # ALIŞ TARİHİ, SATIŞ TARİHİ
                    leftover_qty, buy_price, "",
                    lb_fee, "",
                    lb_amt, "", "", "", "", STATUS_ACIK,
                ], value_input_option="USER_ENTERED")

        else:
            # Bekleyen satış yok → yeni AÇIK
            kz_ws.append_row([
                token, para_birimi,
                gerceklesme_raw, "",           # ALIŞ TARİHİ, SATIŞ TARİHİ
                buy_qty, buy_price, "",
                buy_fee, "",
                buy_amount, "", "", "", "", STATUS_ACIK,
            ], value_input_option="USER_ENTERED")

    # ── SATIŞ ────────────────────────────────────────────────────────────────
    elif is_sell:
        sell_qty    = _num(data.get("gerceklesen_miktar_token"))
        sell_amount = _num(data.get("gerceklesen_tutar"))
        sell_price  = _num(data.get("gerceklesen_fiyat") or data.get("limit_fiyat"))
        sell_fee    = komisyon_val

        # AÇIK alışları FIFO sırasıyla bul
        open_buys = [
            (i + 1, r)
            for i, r in enumerate(all_rows[1:], start=1)
            if row_matches(r, STATUS_ACIK)
        ]

        if not open_buys:
            # Açık alış yok → UNMATCHED (miktar/tutar/kom ALIŞ sütunlarında sakla, SATIŞ TARİHİ'ne tarih)
            kz_ws.append_row([
                token, para_birimi,
                "", gerceklesme_raw,           # ALIŞ TARİHİ boş, SATIŞ TARİHİ dolu
                sell_qty, "", sell_price,
                "", sell_fee,
                "", sell_amount, "", "", "", UNMATCHED,
            ], value_input_option="USER_ENTERED")
            return is_buy, is_sell

        # FIFO: sırayla AÇIK alışları tüket
        remaining_sell     = sell_qty
        remaining_sell_amt = sell_amount

        for buy_idx, buy_row in open_buys:
            if remaining_sell <= 0.000001:
                break

            sheet_row  = buy_idx + 1
            avail_qty  = _num(buy_row[C["ALIŞ MİKTAR"]])
            avail_amt  = _num(buy_row[C["ALIŞ TUTAR"]])
            avail_fee  = _num(buy_row[C["ALIŞ KOM"]])
            buy_price_r = _num(buy_row[C["ALIŞ FİYAT"]])
            buy_date    = buy_row[C["ALIŞ TARİHİ"]]

            if avail_qty <= 0:
                continue

            matched   = min(remaining_sell, avail_qty)
            b_ratio   = matched / avail_qty
            s_ratio   = matched / sell_qty if sell_qty > 0 else 1.0

            used_b_amt = round(avail_amt * b_ratio, 4)
            used_b_fee = round(avail_fee * b_ratio, 4)
            used_s_amt = round(sell_amount * s_ratio, 4)
            used_s_fee = round(sell_fee    * s_ratio, 4)
            brut, pct, net, status = calc_pnl(used_b_amt, used_b_fee, used_s_amt, used_s_fee)

            is_full_match = abs(matched - avail_qty) < 0.000001

            if is_full_match:
                # Tam eşleşme: mevcut AÇIK satırı kapat
                kz_ws.update(
                    f"A{sheet_row}:{LAST_COL}{sheet_row}",
                    [[
                        token, para_birimi,
                        buy_date, gerceklesme_raw,
                        matched, buy_price_r, sell_price,
                        used_b_fee, used_s_fee,
                        used_b_amt, used_s_amt,
                        brut, pct, net, status,
                    ]],
                    value_input_option="USER_ENTERED",
                )
            else:
                # Kısmi eşleşme:
                # 1) Mevcut AÇIK satırı kalan miktarla güncelle (yerinde)
                leftover_qty = round(avail_qty - matched, 8)
                leftover_amt = round(avail_amt - used_b_amt, 4)
                leftover_fee = round(avail_fee - used_b_fee, 4)
                kz_ws.update(
                    f"A{sheet_row}:{LAST_COL}{sheet_row}",
                    [[
                        token, para_birimi,
                        buy_date, "",
                        leftover_qty, buy_price_r, "",
                        leftover_fee, "",
                        leftover_amt, "", "", "", "", STATUS_ACIK,
                    ]],
                    value_input_option="USER_ENTERED",
                )
                # 2) Kapatılan kısım → yeni satır olarak ekle (boşluk yok, sona eklenir)
                kz_ws.append_row([
                    token, para_birimi,
                    buy_date, gerceklesme_raw,
                    matched, buy_price_r, sell_price,
                    used_b_fee, used_s_fee,
                    used_b_amt, used_s_amt,
                    brut, pct, net, status,
                ], value_input_option="USER_ENTERED")

            remaining_sell     -= matched
            remaining_sell_amt -= used_s_amt

        # Satıştan hâlâ kalan → UNMATCHED
        if remaining_sell > 0.000001:
            s_ratio   = remaining_sell / sell_qty if sell_qty > 0 else 1.0
            rem_s_fee = round(sell_fee * s_ratio, 4)
            kz_ws.append_row([
                token, para_birimi,
                "", gerceklesme_raw,
                remaining_sell, "", sell_price,
                "", rem_s_fee,
                "", round(remaining_sell_amt, 4), "", "", "", UNMATCHED,
            ], value_input_option="USER_ENTERED")

    return is_buy, is_sell


# ─── Telegram handlers ────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USERS != {""} and user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Erişim izniniz yok.")
        return

    await update.message.reply_text("📸 Görsel alındı, işleniyor...")

    photo     = await update.message.photo[-1].get_file()
    img_bytes = await photo.download_as_bytearray()

    try:
        data = extract_trade_data(bytes(img_bytes))
        is_buy, is_sell = append_to_sheet(data)

        para_birimi = get_currency_label(data.get("para_birimi", "USDT"))
        cur_sym     = "₺" if para_birimi == "TL" else "$"
        emir_tipi   = data.get("emir_tipi", "")
        token       = data.get("token", "")
        fiyat       = data.get("gerceklesen_fiyat") or data.get("limit_fiyat") or ""
        miktar      = data.get("gerceklesen_miktar_token") or ""
        tutar       = data.get("gerceklesen_tutar") or ""
        komisyon    = data.get("komisyon") or ""
        toplam      = data.get("toplam") or ""

        icon = "🟢" if is_buy else "🔴" if is_sell else "📊"
        msg = (
            f"{icon} *{emir_tipi}* kaydedildi!\n\n"
            f"🪙 Token: `{token}`\n"
            f"💱 Para Birimi: `{para_birimi}`\n"
            f"💲 Fiyat: `{cur_sym}{fiyat}`\n"
            f"📦 Miktar: `{miktar}`\n"
            f"💰 Tutar: `{cur_sym}{tutar}`\n"
            f"🏷 Komisyon: `{cur_sym}{komisyon}`\n"
            f"✅ Toplam: `{cur_sym}{toplam}`\n\n"
        )
        if is_sell:
            msg += "📊 Kar-Zarar hesaplandı ve eşleştirildi!"
        elif is_buy:
            msg += "📝 Kar-Zarar sekmesine AÇIK olarak eklendi."

        await update.message.reply_text(msg, parse_mode="Markdown")

    except json.JSONDecodeError:
        await update.message.reply_text("❌ Veri okunamadı. Lütfen net bir ekran görüntüsü gönderin.")
    except Exception as e:
        log.exception("Hata")
        await update.message.reply_text(f"⚠️ Hata: {e}")


# ─── Komutlar ────────────────────────────────────────────────────────────────
def _auth(update) -> bool:
    user_id = str(update.effective_user.id)
    return ALLOWED_USERS == {""} or user_id in ALLOWED_USERS


async def cmd_acik(update, ctx):
    if not _auth(update):
        await update.message.reply_text("⛔ Erişim izniniz yok.")
        return
    try:
        gc  = sheets_client()
        sh  = gc.open_by_key(SHEET_ID)
        ws  = sh.worksheet("Kar-Zarar")
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
            pb     = _safe(r, C["PARA BİRİMİ"])
            cur    = "₺" if pb == "TL" else "$"
            tok    = _safe(r, C["TOKEN"])
            fiyat  = _safe(r, C["ALIŞ FİYAT"])
            miktar = _safe(r, C["ALIŞ MİKTAR"])
            tutar  = _safe(r, C["ALIŞ TUTAR"])
            lines.append(f"🪙 *{tok}* ({pb})")
            lines.append(f"   Alış: `{cur}{fiyat}` × `{miktar}`")
            lines.append(f"   Tutar: `{cur}{tutar}`\n")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except gspread.WorksheetNotFound:
        await update.message.reply_text("📭 Henüz kayıt yok.")
    except Exception as e:
        log.exception("Hata")
        await update.message.reply_text(f"⚠️ Hata: {e}")


def get_pnl_rows(para_birimi_filter):
    gc  = sheets_client()
    sh  = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("Kar-Zarar")
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    return [r for r in rows[1:] if len(r) > C["PARA BİRİMİ"] and r[C["PARA BİRİMİ"]] == para_birimi_filter]


async def cmd_ozet(update, ctx, para_birimi):
    if not _auth(update):
        await update.message.reply_text("⛔ Erişim izniniz yok.")
        return
    cur = "₺" if para_birimi == "TL" else "$"
    try:
        rows = get_pnl_rows(para_birimi)
        if not rows:
            await update.message.reply_text(f"📭 {para_birimi} işlemi bulunamadı.")
            return

        kapali       = [r for r in rows if len(r) > C["DURUM"] and r[C["DURUM"]] not in (STATUS_ACIK, UNMATCHED)]
        acik         = [r for r in rows if len(r) > C["DURUM"] and r[C["DURUM"]] == STATUS_ACIK]
        toplam_net   = sum(_num(r[C["NET K/Z"]])   for r in kapali if len(r) > C["NET K/Z"])
        toplam_brut  = sum(_num(r[C["BRÜT K/Z"]])  for r in kapali if len(r) > C["BRÜT K/Z"])
        kar_sayisi   = sum(1 for r in kapali if len(r) > C["DURUM"] and "KAR"   in r[C["DURUM"]])
        zarar_sayisi = sum(1 for r in kapali if len(r) > C["DURUM"] and "ZARAR" in r[C["DURUM"]])
        acik_tutar   = sum(_num(r[C["ALIŞ TUTAR"]]) for r in acik  if len(r) > C["ALIŞ TUTAR"])
        icon = "📈" if toplam_net >= 0 else "📉"
        lines = [
            f"{icon} *{para_birimi} Özeti*\n",
            f"💰 Net K/Z: `{cur}{round(toplam_net, 2)}`",
            f"📊 Brüt K/Z: `{cur}{round(toplam_brut, 2)}`\n",
            f"✅ Kârlı işlem: `{kar_sayisi}`",
            f"❌ Zararlı işlem: `{zarar_sayisi}`",
            f"🗂 Açık pozisyon: `{len(acik)}`",
            f"💼 Açık tutar: `{cur}{round(acik_tutar, 2)}`",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        log.exception("Hata")
        await update.message.reply_text(f"⚠️ Hata: {e}")


async def cmd_ozet_usdt(update, ctx):
    await cmd_ozet(update, ctx, "USDT")


async def cmd_ozet_tl(update, ctx):
    await cmd_ozet(update, ctx, "TL")


async def cmd_token(update, ctx):
    if not _auth(update):
        await update.message.reply_text("⛔ Erişim izniniz yok.")
        return
    if not ctx.args:
        await update.message.reply_text("Kullanım: /token ELIZAOS")
        return
    aranan = ctx.args[0].upper()
    try:
        gc   = sheets_client()
        sh   = gc.open_by_key(SHEET_ID)
        ws   = sh.worksheet("Kar-Zarar")
        rows = ws.get_all_values()
        if len(rows) < 2:
            await update.message.reply_text("📭 Kayıt yok.")
            return

        eslesen = [r for r in rows[1:] if len(r) > C["TOKEN"] and r[C["TOKEN"]].upper() == aranan]
        if not eslesen:
            await update.message.reply_text(f"❌ `{aranan}` için kayıt bulunamadı.", parse_mode="Markdown")
            return

        lines = [f"🔍 *{aranan} Geçmişi*\n"]
        for r in eslesen:
            pb    = _safe(r, C["PARA BİRİMİ"])
            cur   = "₺" if pb == "TL" else "$"
            durum = _safe(r, C["DURUM"])
            a_tar = _safe(r, C["ALIŞ TARİHİ"])
            a_fiy = _safe(r, C["ALIŞ FİYAT"])
            a_mik = _safe(r, C["ALIŞ MİKTAR"])
            net   = _safe(r, C["NET K/Z"])
            pct   = _safe(r, C["K/Z %"])

            if durum == STATUS_ACIK:
                lines.append(f"🗂 *AÇIK* | {pb}")
                lines.append(f"   Alış: `{a_tar}` @ `{cur}{a_fiy}` × `{a_mik}`\n")
            elif durum == UNMATCHED:
                s_tar = _safe(r, C["SATIŞ TARİHİ"])
                s_mik = _safe(r, C["ALIŞ MİKTAR"])
                lines.append(f"⚠️ EŞLEŞMEDİ | {pb}")
                lines.append(f"   Satış: `{s_tar}` × `{s_mik}`\n")
            else:
                s_tar = _safe(r, C["SATIŞ TARİHİ"])
                lines.append(f"{durum} | {pb}")
                lines.append(f"   Alış: `{a_tar}` @ `{cur}{a_fiy}`")
                lines.append(f"   Satış: `{s_tar}`")
                lines.append(f"   Net K/Z: `{cur}{net}` (`%{pct}`)\n")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except gspread.WorksheetNotFound:
        await update.message.reply_text("📭 Kayıt yok.")
    except Exception as e:
        log.exception("Hata")
        await update.message.reply_text(f"⚠️ Hata: {e}")


async def cmd_yardim(update, ctx):
    if not _auth(update):
        await update.message.reply_text("⛔ Erişim izniniz yok.")
        return
    msg = (
        "🤖 *Kripto İşlem Kayıt Botu*\n\n"
        "📸 *Nasıl kullanılır?*\n"
        "Herhangi bir borsadan işlem ekran görüntüsü atın, bot otomatik kaydeder.\n\n"
        "📊 *Sorgulama Komutları*\n"
        "/acik — Tüm açık pozisyonları listeler\n"
        "/ozet_usdt — USDT işlemlerinin toplam kar/zarar özeti\n"
        "/ozet_tl — TL işlemlerinin toplam kar/zarar özeti\n"
        "/token ELIZAOS — Belirli bir tokenin tüm alış/satış geçmişi\n\n"
        "🗑 *Silme Komutları*\n"
        "/sil — Son kaydedilen işlemi siler\n"
        "/sil ELIZAOS — O tokena ait son kaydı siler\n"
        "/sifirla — Tüm verileri siler (çift onay gerektirir)\n\n"
        "ℹ️ *Bilgi*\n"
        "USDT ve TL işlemleri ayrı takip edilir.\n"
        "Çok alış → tek/kısmi satış senaryoları desteklenir.\n"
        "Satışı alıştan önce atsan da otomatik eşleştirilir."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ─── Silme komutları ──────────────────────────────────────────────────────────
async def cmd_sil(update, ctx):
    if not _auth(update):
        await update.message.reply_text("⛔ Erişim izniniz yok.")
        return
    _sifirla_onay.pop(str(update.effective_user.id), None)
    token_filtre = ctx.args[0].upper() if ctx.args else None
    try:
        gc = sheets_client()
        sh = gc.open_by_key(SHEET_ID)

        for sekme, token_col in [("İşlem Kayıtları", 3), ("Kar-Zarar", C["TOKEN"])]:
            try:
                ws   = sh.worksheet(sekme)
                rows = ws.get_all_values()
                for i in range(len(rows) - 1, 0, -1):
                    r = rows[i]
                    if token_filtre is None or (len(r) > token_col and r[token_col].upper() == token_filtre):
                        ws.delete_rows(i + 1)
                        break
            except gspread.WorksheetNotFound:
                pass

        label = f"`{token_filtre}` son kaydı" if token_filtre else "Son kayıt"
        await update.message.reply_text(f"✅ {label} silindi.", parse_mode="Markdown")
    except Exception as e:
        log.exception("Hata")
        await update.message.reply_text(f"⚠️ Hata: {e}")


_sifirla_onay = {}


async def cmd_sifirla(update, ctx):
    if not _auth(update):
        await update.message.reply_text("⛔ Erişim izniniz yok.")
        return
    user_id = str(update.effective_user.id)

    if _sifirla_onay.get(user_id):
        _sifirla_onay.pop(user_id, None)
        try:
            gc = sheets_client()
            sh = gc.open_by_key(SHEET_ID)
            for sekme in ["İşlem Kayıtları", "Kar-Zarar"]:
                try:
                    sh.del_worksheet(sh.worksheet(sekme))
                except gspread.WorksheetNotFound:
                    pass
            await update.message.reply_text("🗑 Tüm veriler silindi.")
        except Exception as e:
            log.exception("Hata")
            await update.message.reply_text(f"⚠️ Hata: {e}")
    else:
        _sifirla_onay[user_id] = True
        await update.message.reply_text(
            "⚠️ *Tüm veriler silinecek!*\n\n"
            "Emin misiniz? Onaylamak için tekrar /sifirla yazın.\n"
            "Vazgeçmek için herhangi bir şey yazın.",
            parse_mode="Markdown",
        )


async def handle_text(update, ctx):
    user_id = str(update.effective_user.id)
    if _sifirla_onay.pop(user_id, None):
        await update.message.reply_text("❌ Sıfırlama iptal edildi.")
        return

    await update.message.reply_text(
        "👋 Merhaba! İşlem detayı ekran görüntüsü gönderin, otomatik kaydedeyim.\n\n"
        "📸 Desteklenen: Binance, Bybit, OKX, BtcTurk, Paribu ve diğer tüm borsalar.\n"
        "💱 USDT ve TL işlemleri desteklenir.\n\n"
        "📊 *Komutlar:*\n"
        "/acik — Açık pozisyonlar\n"
        "/ozet_usdt — USDT özeti\n"
        "/ozet_tl — TL özeti\n"
        "/token ELIZAOS — Token geçmişi\n"
        "/sil — Son kaydı sil\n"
        "/sil ELIZAOS — Token son kaydını sil\n"
        "/sifirla — Tüm verileri sil\n"
        "/yardim — Yardım",
        parse_mode="Markdown",
    )


# ─── main ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("acik",      cmd_acik))
    app.add_handler(CommandHandler("ozet_usdt", cmd_ozet_usdt))
    app.add_handler(CommandHandler("ozet_tl",   cmd_ozet_tl))
    app.add_handler(CommandHandler("token",     cmd_token))
    app.add_handler(CommandHandler("sil",       cmd_sil))
    app.add_handler(CommandHandler("sifirla",   cmd_sifirla))
    app.add_handler(CommandHandler("yardim",    cmd_yardim))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot başlatıldı...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
