import os
import json
import base64
import logging
import anthropic
import gspread
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
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


def sheets_client():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def extract_trade_data(image_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": EXTRACT_PROMPT},
            ],
        }],
    )
    raw = msg.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def classify_order(emir_tipi: str) -> tuple[bool, bool]:
    """Return (is_buy, is_sell). Mutually exclusive."""
    et = (emir_tipi or "").lower()
    # Satış kontrolü önce — "alış satış" gibi garip değerlerde satışı öncelikle yakala
    is_sell = any(k in et for k in ["satış", "satish", "sell"])
    is_buy  = not is_sell and any(k in et for k in ["alış", "alish", "buy"])
    return is_buy, is_sell


def get_currency_label(para_birimi: str) -> str:
    return "TL" if (para_birimi or "").upper() == "TL" else "USDT"


def append_to_sheet(data: dict) -> tuple[bool, bool]:
    gc = sheets_client()
    sh = gc.open_by_key(SHEET_ID)

    para_birimi = get_currency_label(data.get("para_birimi", "USDT"))
    emir_tipi   = data.get("emir_tipi", "")
    token       = (data.get("token") or "").strip()
    is_buy, is_sell = classify_order(emir_tipi)

    # ── İşlem Kayıtları sekmesi ──────────────────────────────────────────────
    try:
        ws = sh.worksheet("İşlem Kayıtları")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("İşlem Kayıtları", rows=1000, cols=20)
        ws.append_row([
            "PLATFORM", "İŞLEM ÇİFTİ", "PARA BİRİMİ", "EMİR TİPİ", "DURUM",
            "EMİR TARİHİ", "GERÇEKLEŞME TARİHİ",
            "GİRİLEN ADET", "LİMİT FİYAT",
            "GERÇEKLEŞEN MİKTAR (TOKEN)", "GERÇEKLEŞEN FİYAT",
            "GERÇEKLEŞEN TUTAR", "KOMİSYON", "TOPLAM", "NOT",
        ])

    fiyat_label = f"FİYAT ({para_birimi})"  # noqa: F841  (for future column rename use)

    row = [
        data.get("platform")                  or "",
        data.get("islem_cifti")               or "",
        para_birimi,
        emir_tipi,
        data.get("durum")                     or "",
        data.get("emir_tarihi")               or "",
        data.get("gerceklesme_tarihi")        or "",
        data.get("girilen_adet")              or "",
        data.get("limit_fiyat")               or "",
        data.get("gerceklesen_miktar_token")  or "",
        data.get("gerceklesen_fiyat")         or "",
        data.get("gerceklesen_tutar")         or "",
        data.get("komisyon")                  or "",
        data.get("toplam")                    or "",
        f"Bot · {datetime.now().strftime('%d.%m.%Y %H:%M')}",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")

    # ── Kar-Zarar sekmesi ────────────────────────────────────────────────────
    try:
        pnl_ws = sh.worksheet("Kar-Zarar")
    except gspread.WorksheetNotFound:
        pnl_ws = sh.add_worksheet("Kar-Zarar", rows=500, cols=20)
        pnl_ws.append_row([
            "TOKEN", "PARA BİRİMİ",
            "ALIŞ TARİHİ", "ALIŞ FİYAT", "ALIŞ MİKTAR", "ALIŞ TUTAR", "ALIŞ KOM",
            "SATIŞ TARİHİ", "SATIŞ FİYAT", "SATIŞ MİKTAR", "SATIŞ TUTAR", "SATIŞ KOM",
            "BRÜT K/Z", "K/Z %", "NET K/Z", "DURUM",
        ])
        # Col indices (0-based): TOKEN=0, PARA_BIRIMI=1, ... DURUM=15

    if is_buy:
        pnl_ws.append_row([
            token,
            para_birimi,
            data.get("gerceklesme_tarihi") or data.get("emir_tarihi") or "",
            data.get("gerceklesen_fiyat")  or data.get("limit_fiyat") or "",
            data.get("gerceklesen_miktar_token") or "",
            data.get("gerceklesen_tutar") or "",
            data.get("komisyon") or "",
            "", "", "", "", "",   # satış alanları boş
            "", "", "",
            "AÇIK",
        ], value_input_option="USER_ENTERED")

    elif is_sell and token:
        all_rows = pnl_ws.get_all_values()
        headers  = all_rows[0] if all_rows else []

        # Sütun index tespiti (başlık satırından) — sabit index yerine dinamik
        def col(name):
            try:
                return headers.index(name)
            except ValueError:
                return None

        TOKEN_COL   = col("TOKEN")         # 0
        PB_COL      = col("PARA BİRİMİ")  # 1
        STATUS_COL  = col("DURUM")        # 15

        matched_row_idx = None  # data satır index'i (all_rows içinde, 0-based)
        for i, r in enumerate(all_rows[1:], start=1):   # i=1 → data row 2 in sheet
            # FIFO: aynı token + aynı para birimi + AÇIK olan en eski satır
            token_match = TOKEN_COL is not None and len(r) > TOKEN_COL and r[TOKEN_COL] == token
            pb_match    = PB_COL    is not None and len(r) > PB_COL    and r[PB_COL] == para_birimi
            status_open = STATUS_COL is not None and len(r) > STATUS_COL and r[STATUS_COL] == "AÇIK"
            if token_match and pb_match and status_open:
                matched_row_idx = i
                break   # FIFO: ilk (en eski) eşleşme

        if matched_row_idx is not None:
            r = all_rows[matched_row_idx]

            sell_price  = _num(data.get("gerceklesen_fiyat") or data.get("limit_fiyat"))
            sell_qty    = _num(data.get("gerceklesen_miktar_token"))
            sell_amount = _num(data.get("gerceklesen_tutar"))
            sell_fee    = _num(data.get("komisyon"))

            buy_amount  = _num(r[5]) if len(r) > 5 else 0.0  # ALIŞ TUTAR
            buy_fee     = _num(r[6]) if len(r) > 6 else 0.0  # ALIŞ KOM

            brut_pnl = sell_amount - buy_amount
            pct      = (brut_pnl / buy_amount * 100) if buy_amount else 0.0
            net_pnl  = brut_pnl - buy_fee - sell_fee
            status   = "KAR ✅" if net_pnl > 0 else "ZARAR ❌"

            sheet_row = matched_row_idx + 1  # gspread 1-based
            pnl_ws.update(
                f"H{sheet_row}:P{sheet_row}",
                [[
                    data.get("gerceklesme_tarihi") or "",
                    sell_price, sell_qty, sell_amount, sell_fee,
                    round(brut_pnl, 4),
                    round(pct, 2),
                    round(net_pnl, 4),
                    status,
                ]],
                value_input_option="USER_ENTERED",
            )
        else:
            # Eşleşen alış bulunamadı — yine de Kar-Zarar'a yaz, ALIŞ alanları boş
            pnl_ws.append_row([
                token, para_birimi,
                "", "", "", "", "",  # alış alanları boş
                data.get("gerceklesme_tarihi") or data.get("emir_tarihi") or "",
                data.get("gerceklesen_fiyat") or "",
                data.get("gerceklesen_miktar_token") or "",
                data.get("gerceklesen_tutar") or "",
                data.get("komisyon") or "",
                "", "", "",
                "EŞLEŞMEDİ ⚠️",
            ], value_input_option="USER_ENTERED")

    return is_buy, is_sell


def _num(val) -> float:
    """Güvenli float dönüşümü. None, '', boşluk → 0.0"""
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", ".").strip())
    except (ValueError, TypeError):
        return 0.0


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USERS != {""} and user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Erişim izniniz yok.")
        return

    await update.message.reply_text("📸 Görsel alındı, işleniyor...")

    photo = await update.message.photo[-1].get_file()
    img_bytes = await photo.download_as_bytearray()

    try:
        data = extract_trade_data(bytes(img_bytes))
        is_buy, is_sell = append_to_sheet(data)

        para_birimi = get_currency_label(data.get("para_birimi", "USDT"))
        cur_sym     = "₺" if para_birimi == "TL" else "$"

        emir_tipi = data.get("emir_tipi", "")
        token     = data.get("token", "")
        fiyat     = data.get("gerceklesen_fiyat") or data.get("limit_fiyat") or ""
        miktar    = data.get("gerceklesen_miktar_token") or ""
        tutar     = data.get("gerceklesen_tutar") or ""
        komisyon  = data.get("komisyon") or ""
        toplam    = data.get("toplam") or ""

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


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Merhaba! İşlem detayı ekran görüntüsü gönderin, otomatik kaydedeyim.\n\n"
        "📸 Desteklenen: Binance, Bybit, OKX, BtcTurk, Paribu ve diğer tüm borsalar.\n"
        "💱 USDT ve TL işlemleri desteklenir."
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot başlatıldı...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
