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
        model="claude-sonnet-4-6",
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
        ws = sh.add_worksheet("İşlem Kayıtları", rows=1000, cols=10)
        ws.append_row([
            "Gerçekleşme Tarihi", "Gerçekleşme Saati",
            "Emir Tipi", "Ürün",
            "Token Adet", "Fiyat Maliyet", "Maliyet Birim",
            "Komisyon", "Gerçekleşen Tutar", "Gerçekleşen Tutar Birim",
        ])

    # Tarih ve saati böl: "5 Mayıs 2026, 21:44:26" → ("5 Mayıs 2026", "21:44:26")
    gerceklesme_raw = data.get("gerceklesme_tarihi") or data.get("emir_tarihi") or ""
    if "," in gerceklesme_raw:
        tarih_part, saat_part = [x.strip() for x in gerceklesme_raw.split(",", 1)]
    else:
        tarih_part = gerceklesme_raw
        saat_part  = ""

    # Komisyon: sayısal değer varsa sayıyı yaz, metin (örn. "Ücretsiz") veya null ise 0 yaz
    komisyon_raw = data.get("komisyon")
    try:
        komisyon_val = float(str(komisyon_raw).replace(",", ".").strip()) if komisyon_raw is not None else 0
    except (ValueError, TypeError):
        komisyon_val = 0

    row = [
        tarih_part,
        saat_part,
        emir_tipi,
        data.get("token") or "",
        data.get("gerceklesen_miktar_token") or "",
        data.get("gerceklesen_fiyat") or data.get("limit_fiyat") or "",
        para_birimi,
        komisyon_val,
        data.get("gerceklesen_tutar") or "",
        para_birimi,
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
        "💱 USDT ve TL işlemleri desteklenir.\n\n"
        "📊 *Komutlar:*\n"
        "/acik — Açık pozisyonlar\n"
        "/ozet_usdt — USDT özeti\n"
        "/ozet_tl — TL özeti\n"
        "/token ELIZAOS — Token geçmişi"
    )




def get_pnl_rows(para_birimi_filter):
    gc = sheets_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("Kar-Zarar")
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    headers = rows[0]
    try:
        pb_col = headers.index("PARA B\u0130R\u0130M\u0130")
    except ValueError:
        pb_col = 1
    return [r for r in rows[1:] if len(r) > pb_col and r[pb_col] == para_birimi_filter]


def _safe(row, idx, default="?"):
    return row[idx] if len(row) > idx else default


async def cmd_acik(update, ctx):
    user_id = str(update.effective_user.id)
    if ALLOWED_USERS != {""} and user_id not in ALLOWED_USERS:
        await update.message.reply_text("\u26d4 Eri\u015fim izniniz yok.")
        return
    try:
        gc = sheets_client()
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.worksheet("Kar-Zarar")
        rows = ws.get_all_values()
        if len(rows) < 2:
            await update.message.reply_text("\U0001f4ed Hen\u00fcz kay\u0131t yok.")
            return
        headers = rows[0]
        try:
            status_col = headers.index("DURUM")
            token_col  = headers.index("TOKEN")
            pb_col     = headers.index("PARA B\u0130R\u0130M\u0130")
            fiyat_col  = headers.index("ALI\u015e F\u0130YAT")
            miktar_col = headers.index("ALI\u015e M\u0130KTAR")
            tutar_col  = headers.index("ALI\u015e TUTAR")
        except ValueError:
            status_col, token_col, pb_col, fiyat_col, miktar_col, tutar_col = 15, 0, 1, 3, 4, 5

        acik = [r for r in rows[1:] if len(r) > status_col and r[status_col] == "A\u00c7IK"]
        if not acik:
            await update.message.reply_text("\u2705 A\u00e7\u0131k pozisyon yok.")
            return

        lines = ["\U0001f5c2 *A\u00e7\u0131k Pozisyonlar*\n"]
        for r in acik:
            token  = _safe(r, token_col)
            pb     = _safe(r, pb_col)
            fiyat  = _safe(r, fiyat_col)
            miktar = _safe(r, miktar_col)
            tutar  = _safe(r, tutar_col)
            cur    = "\u20ba" if pb == "TL" else "$"
            lines.append(f"\U0001fa99 *{token}* ({pb})")
            lines.append(f"   Al\u0131\u015f: `{cur}{fiyat}` \u00d7 `{miktar}`")
            lines.append(f"   Tutar: `{cur}{tutar}`\n")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        log.exception("Hata")
        await update.message.reply_text(f"\u26a0\ufe0f Hata: {e}")


async def cmd_ozet(update, ctx, para_birimi):
    user_id = str(update.effective_user.id)
    if ALLOWED_USERS != {""} and user_id not in ALLOWED_USERS:
        await update.message.reply_text("\u26d4 Eri\u015fim izniniz yok.")
        return
    cur = "\u20ba" if para_birimi == "TL" else "$"
    try:
        rows = get_pnl_rows(para_birimi)
        if not rows:
            await update.message.reply_text(f"\U0001f4ed {para_birimi} i\u015flemi bulunamad\u0131.")
            return
        kapali      = [r for r in rows if len(r) > 15 and r[15] not in ("A\u00c7IK", "E\u015eLe\u015eMED\u0130 \u26a0\ufe0f")]
        acik        = [r for r in rows if len(r) > 15 and r[15] == "A\u00c7IK"]
        toplam_net  = sum(_num(r[14]) for r in kapali if len(r) > 14)
        toplam_brut = sum(_num(r[12]) for r in kapali if len(r) > 12)
        kar_sayisi  = sum(1 for r in kapali if len(r) > 15 and "KAR" in r[15])
        zarar_sayisi= sum(1 for r in kapali if len(r) > 15 and "ZARAR" in r[15])
        acik_tutar  = sum(_num(r[5]) for r in acik if len(r) > 5)
        icon = "\U0001f4c8" if toplam_net >= 0 else "\U0001f4c9"
        lines = [
            f"{icon} *{para_birimi} \u00d6zeti*\n",
            f"\U0001f4b0 Net K/Z: `{cur}{round(toplam_net, 2)}`",
            f"\U0001f4ca Br\u00fct K/Z: `{cur}{round(toplam_brut, 2)}`\n",
            f"\u2705 K\u00e2rl\u0131 i\u015flem: `{kar_sayisi}`",
            f"\u274c Zarafl\u0131 i\u015flem: `{zarar_sayisi}`",
            f"\U0001f5c2 A\u00e7\u0131k pozisyon: `{len(acik)}`",
            f"\U0001f4bc A\u00e7\u0131k tutar: `{cur}{round(acik_tutar, 2)}`",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        log.exception("Hata")
        await update.message.reply_text(f"\u26a0\ufe0f Hata: {e}")


async def cmd_ozet_usdt(update, ctx):
    await cmd_ozet(update, ctx, "USDT")


async def cmd_ozet_tl(update, ctx):
    await cmd_ozet(update, ctx, "TL")


async def cmd_token(update, ctx):
    user_id = str(update.effective_user.id)
    if ALLOWED_USERS != {""} and user_id not in ALLOWED_USERS:
        await update.message.reply_text("\u26d4 Eri\u015fim izniniz yok.")
        return
    if not ctx.args:
        await update.message.reply_text("Kullan\u0131m: /token ELIZAOS")
        return
    aranan = ctx.args[0].upper()
    try:
        gc = sheets_client()
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.worksheet("Kar-Zarar")
        rows = ws.get_all_values()
        if len(rows) < 2:
            await update.message.reply_text("\U0001f4ed Kay\u0131t yok.")
            return
        eslesen = [r for r in rows[1:] if len(r) > 0 and r[0].upper() == aranan]
        if not eslesen:
            await update.message.reply_text(f"\u274c `{aranan}` i\u00e7in kay\u0131t bulunamad\u0131.", parse_mode="Markdown")
            return
        lines = [f"\U0001f50d *{aranan} Ge\u00e7mi\u015fi*\n"]
        for r in eslesen:
            pb    = _safe(r, 1)
            cur   = "\u20ba" if pb == "TL" else "$"
            durum = _safe(r, 15)
            a_tar = _safe(r, 2)
            a_fiy = _safe(r, 3)
            a_mik = _safe(r, 4)
            net   = _safe(r, 14)
            pct   = _safe(r, 13)
            if durum == "A\u00c7IK":
                lines.append(f"\U0001f5c2 *A\u00c7IK* | {pb}")
                lines.append(f"   Al\u0131\u015f: `{a_tar}` @ `{cur}{a_fiy}` \u00d7 `{a_mik}`\n")
            else:
                s_tar = _safe(r, 7)
                lines.append(f"{durum} | {pb}")
                lines.append(f"   Al\u0131\u015f: `{a_tar}` @ `{cur}{a_fiy}`")
                lines.append(f"   Sat\u0131\u015f: `{s_tar}`")
                lines.append(f"   Net K/Z: `{cur}{net}` (`%{pct}`)\n")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        log.exception("Hata")
        await update.message.reply_text(f"\u26a0\ufe0f Hata: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('acik', cmd_acik))
    app.add_handler(CommandHandler('ozet_usdt', cmd_ozet_usdt))
    app.add_handler(CommandHandler('ozet_tl', cmd_ozet_tl))
    app.add_handler(CommandHandler('token', cmd_token))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot başlatıldı...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
