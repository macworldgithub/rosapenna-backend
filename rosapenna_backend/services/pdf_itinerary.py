"""PDF itinerary generator for Rosapenna Hotel & Golf Resort bookings."""

from __future__ import annotations

import datetime as dt
import html
import io
import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Dict
from xml.sax.saxutils import escape as xml_escape

if TYPE_CHECKING:
    from rosapenna_backend.api.schemas.communications import BookingConfirmationSnapshot

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Flowable,
    Frame,
    HRFlowable,
    KeepTogether,
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Brand colors
GOLD = colors.HexColor("#DBB155")
DARK_GREEN = colors.HexColor("#1A3323")
LIGHT_BG = colors.HexColor("#F8F9FA")
BORDER_COLOR = colors.HexColor("#E2E8F0")
TEXT_MUTED = colors.HexColor("#64748B")
TEXT_DARK = colors.HexColor("#0F172A")


def build_itinerary_pdf(booking: Dict[str, Any]) -> bytes:
    """Render a comprehensive, professional PDF booking itinerary.

    Includes lead guest details, booking reference, room reservation,
    championship golf rounds, verified payments, and balance due dates.
    Supports canonical snapshots from both BookingIntentStore and BookingLinkageService.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=DARK_GREEN,
    )
    subtitle_style = ParagraphStyle(
        "DocSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=TEXT_MUTED,
    )
    section_heading = ParagraphStyle(
        "SectionHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=17,
        textColor=DARK_GREEN,
        spaceAfter=6,
    )
    label_style = ParagraphStyle(
        "LabelStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
        textColor=TEXT_DARK,
    )
    value_style = ParagraphStyle(
        "ValueStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=TEXT_DARK,
    )
    table_cell = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=TEXT_DARK,
    )
    table_header = ParagraphStyle(
        "TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=colors.white,
    )
    footer_style = ParagraphStyle(
        "FooterStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=TEXT_MUTED,
        alignment=1,  # Center
    )

    elements = []

    # 1. Header with resort branding
    ref = booking.get("booking_reference") or booking.get("booking_intent_id", "PENDING")
    revision = booking.get("revision_number") or booking.get("booking_revision")
    revision_text = f" (Revision #{revision})" if revision and int(revision) > 0 else ""
    stage_text = booking.get("stage", "Confirmed").replace("_", " ").title()

    header_data = [
        [
            Paragraph("ROSAPENNA HOTEL & GOLF RESORT", title_style),
            Paragraph(f"<b>BOOKING REFERENCE</b><br/><font size='12' color='#1A3323'><b>{ref}</b></font>", ParagraphStyle("Ref", alignment=2, fontName="Helvetica", leading=16)),
        ],
        [
            Paragraph("Downings, Sheephaven Bay, Co. Donegal, Ireland &bull; reservations@rosapenna.ie", subtitle_style),
            Paragraph(f"Status: <b>{stage_text}{revision_text}</b>", ParagraphStyle("Status", alignment=2, fontName="Helvetica", fontSize=9, textColor=DARK_GREEN)),
        ],
    ]
    header_table = Table(header_data, colWidths=[340, 200])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 10))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=GOLD, spaceAfter=14))

    # 2. Lead Guest Details
    lead = booking.get("lead_guest") or {}
    guest_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Valued Guest"
    guest_email = lead.get("email", "")
    guest_phone = lead.get("telephone", "")

    guest_data = [
        [
            Paragraph("Lead Guest:", label_style),
            Paragraph(guest_name, value_style),
            Paragraph("Email Address:", label_style),
            Paragraph(guest_email, value_style),
        ],
        [
            Paragraph("Telephone:", label_style),
            Paragraph(guest_phone, value_style),
            Paragraph("Confirmation Date:", label_style),
            Paragraph(dt.date.today().strftime("%d %B %Y"), value_style),
        ],
    ]
    guest_table = Table(guest_data, colWidths=[80, 190, 95, 175])
    guest_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(guest_table)
    elements.append(Spacer(1, 14))

    # 3. Accommodation Details (Opera Cloud / Canonical Snapshot)
    opera_res = (
        booking.get("links", {}).get("opera_reservation")
        or booking.get("opera_stay")
        or booking.get("stay")
        or booking.get("hotel")
    )
    elements.append(Paragraph("Accommodation Details", section_heading))

    arr_date_val = None
    if opera_res:
        arr_date = opera_res.get("arrival_date", "")
        dep_date = opera_res.get("departure_date", "")
        arr_date_val = arr_date
        room_cat = (opera_res.get("room_category") or opera_res.get("room_type") or "Standard Room").replace("_", " ").title()
        room_qty = opera_res.get("room_quantity", 1)
        conf_num = opera_res.get("confirmation_number") or opera_res.get("reservation_id", "")
        occupant_names = opera_res.get("occupant_names") or []

        room_data = [
            [
                Paragraph("Room Category", table_header),
                Paragraph("Arrival Date", table_header),
                Paragraph("Departure Date", table_header),
                Paragraph("Quantity", table_header),
                Paragraph("Opera Conf #", table_header),
            ],
            [
                Paragraph(f"<b>{room_cat}</b><br/><font color='#64748B' size='7.5'>Traditional Irish Breakfast included</font>", table_cell),
                Paragraph(str(arr_date), table_cell),
                Paragraph(str(dep_date), table_cell),
                Paragraph(f"{room_qty} room(s)", table_cell),
                Paragraph(str(conf_num), table_cell),
            ],
        ]
        room_table = Table(room_data, colWidths=[170, 95, 95, 80, 100])
        room_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), DARK_GREEN),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BACKGROUND", (0, 1), (-1, 1), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(room_table)
        elements.append(Spacer(1, 4))

        if occupant_names:
            occ_list_str = ", ".join(occupant_names)
            elements.append(Paragraph(f"<font size='8' color='#334155'><b>Occupants:</b> {occ_list_str}</font>", subtitle_style))
            elements.append(Spacer(1, 2))

        elements.append(Paragraph("<font size='7.5' color='#64748B'>* Note: Hotel allocates physical room numbers approximately 2 days prior to arrival.</font>", subtitle_style))
    else:
        elements.append(Paragraph("No hotel accommodation linked to this booking.", table_cell))

    elements.append(Spacer(1, 14))

    # 4. Championship Golf Rounds (BRS Golf / Canonical Snapshot)
    brs_rounds = (
        booking.get("links", {}).get("brs_rounds")
        or booking.get("brs_rounds")
        or booking.get("golf_rounds")
        or []
    )
    elements.append(Paragraph("Championship Links Golf", section_heading))

    if brs_rounds:
        golf_data = [
            [
                Paragraph("Course", table_header),
                Paragraph("Date", table_header),
                Paragraph("Tee Time", table_header),
                Paragraph("Players", table_header),
                Paragraph("Holes", table_header),
                Paragraph("Caddy Request", table_header),
            ],
        ]
        for r in brs_rounds:
            caddy_info = "None"
            cad_req = r.get("caddy_request")
            if isinstance(cad_req, dict) and cad_req.get("caddies_requested", 0) > 0:
                caddy_info = f"{cad_req['caddies_requested']} {cad_req.get('caddy_category', 'caddie').title()}(s)"
            elif isinstance(cad_req, str):
                caddy_info = cad_req
            elif r.get("caddy_request_status"):
                caddy_info = str(r["caddy_request_status"])

            golf_data.append([
                Paragraph(f"<b>{r.get('course_name', 'Championship Links')}</b>", table_cell),
                Paragraph(str(r.get("date", "")), table_cell),
                Paragraph(str(r.get("time", "")), table_cell),
                Paragraph(f"{r.get('players', 1)} golfer(s)", table_cell),
                Paragraph(str(r.get("holes", "18")), table_cell),
                Paragraph(caddy_info, table_cell),
            ])
        golf_table = Table(golf_data, colWidths=[160, 80, 65, 75, 55, 105])
        golf_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), DARK_GREEN),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(golf_table)
        elements.append(Spacer(1, 4))
        elements.append(Paragraph("<font size='7.5' color='#64748B'>* Note: Caddies are availability requests payable directly to the caddie master on the day of play (EUR 90 per bag).</font>", subtitle_style))
    else:
        elements.append(Paragraph("No golf rounds linked to this booking.", table_cell))

    elements.append(Spacer(1, 14))

    # 5. Financial Summary & Verified Ledger (F05 / Canonical Snapshot)
    elements.append(Paragraph("Payment & Financial Ledger", section_heading))

    # Determine financials
    fin = booking.get("financials") or {}
    total_num = 0.0
    verified_paid = 0.0
    balance_due = 0.0
    balance_due_date_str = ""

    if fin:
        total_num = float(fin.get("total_amount") or fin.get("total_price", 0.0))
        verified_paid = float(fin.get("deposit_paid", 0.0))
        balance_due = float(fin.get("balance_due", 0.0))
        if fin.get("balance_due_date"):
            balance_due_date_str = str(fin["balance_due_date"])
    else:
        # Calculate from quote or selection
        total_val = "0.00"
        if "quote" in booking and "selected" in booking["quote"]:
            total_val = str(booking["quote"]["selected"].get("amount") or booking["quote"]["selected"].get("price", "0.00"))
        elif "selection" in booking:
            total_val = str(booking["selection"].get("price", "0.00"))
        total_num = float(total_val)

        payments = booking.get("links", {}).get("payments") or booking.get("payments") or []
        verified_paid = sum(
            (float(p.get("amount", 0.0)) for p in payments if p.get("status") in {"completed", "captured", "paid"}),
            0.0,
        )
        balance_due = max(0.0, total_num - verified_paid)

    # Calculate authoritative 42-day balance due date if not explicit
    if not balance_due_date_str and arr_date_val:
        try:
            arr_dt = dt.date.fromisoformat(str(arr_date_val).split("T")[0])
            bal_dt = arr_dt - dt.timedelta(days=42)
            balance_due_date_str = bal_dt.strftime("%d %B %Y")
        except Exception:
            pass

    if balance_due_date_str:
        due_label = f"{balance_due_date_str} (Strictly 6 weeks prior)"
    else:
        due_label = "6 Weeks Prior to Arrival"

    finance_data = [
        [
            Paragraph("Total Package Price (inc. VAT):", label_style),
            Paragraph(f"<b>EUR {total_num:,.2f}</b>", value_style),
            Paragraph("Verified Paid to Date:", label_style),
            Paragraph(f"<b>EUR {verified_paid:,.2f}</b>", value_style),
        ],
        [
            Paragraph("Remaining Balance Due:", label_style),
            Paragraph(f"<font color='#B91C1C'><b>EUR {balance_due:,.2f}</b></font>", value_style),
            Paragraph("Balance Due Date:", label_style),
            Paragraph(due_label, value_style),
        ],
    ]
    finance_table = Table(finance_data, colWidths=[170, 100, 150, 120])
    finance_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(finance_table)
    elements.append(Spacer(1, 20))

    # 6. Important Information & Policies
    elements.append(Paragraph("Resort Information & Terms", section_heading))
    policy_text = (
        "<b>Check-In:</b> From 15:00. <b>Check-Out:</b> By 11:00.<br/>"
        "<b>Deposit & Cancellation:</b> A 25% non-refundable deposit applies to all confirmed bookings. "
        "The remaining 75% balance is strictly due 6 weeks (42 days) prior to arrival.<br/>"
        "<b>Golf Dress Code:</b> Traditional golf attire is required. Soft spikes only on all three links courses.<br/>"
        "<b>Need Assistance?</b> Contact Reservations at reservations@rosapenna.ie or +353 (0)74 915 5301."
    )
    elements.append(Paragraph(policy_text, table_cell))
    elements.append(Spacer(1, 15))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_COLOR, spaceAfter=8))
    elements.append(Paragraph("Rosapenna Hotel & Golf Resort &bull; Official Itinerary Document &bull; Generated Securely", footer_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


# ==============================================================================
# Ticket N07: guest booking-confirmation PDF (strict snapshot renderer)
# ==============================================================================

RESORT_NAME = "Rosapenna Hotel & Golf Resort"
RESORT_ADDRESS = "Downings, Sheephaven Bay, Co. Donegal, Ireland"
DEFAULT_RESERVATIONS_EMAIL = "reservations@rosapenna.ie"
RESERVATIONS_PHONE = "+353 (0)74 915 5301"
TEST_BANNER_TITLE = "TEST BOOKING – NOT A REAL RESERVATION"
TEST_LABEL = "TEST — NOT A REAL BOOKING CONFIRMATION"

# Extra palette for the guest confirmation (legacy colours above stay unchanged).
MID_GREEN = colors.HexColor("#2F5D3F")
PALE_GREEN = colors.HexColor("#EEF3EE")
CREAM = colors.HexColor("#F7F3E8")
INK_MUTED = colors.HexColor("#4B5563")  # 7.6:1 on white, 6.9:1 on CREAM
ALERT_RED = colors.HexColor("#B91C1C")
RULE_GREY = colors.HexColor("#D5DBD6")

_PAGE_W, _PAGE_H = A4
_MARGIN = 40
_CONTENT_W = _PAGE_W - 2 * _MARGIN
_FIRST_HEADER_H = 118
_LATER_HEADER_H = 34
_FOOTER_H = 70
_MIN_SECTION_SPACE = 150  # start a table section on a new page when less room than this is left
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}
_PAYMENT_PURPOSES = {"deposit": "Deposit", "balance": "Balance payment", "full": "Payment in full"}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAILBOX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,61}[A-Za-z0-9])?\.[A-Za-z]{2,63}")


def _esc(value: Any) -> str:
    """Escape one value for ReportLab paragraph markup (guest text is never markup)."""
    return xml_escape(_CONTROL_CHARS.sub("", str(value)))


def _esc_lines(value: Any) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return _esc(text).replace("\n", "<br/>")


def _long_date(value: Any, weekday: bool = True) -> str:
    try:
        day = dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return _esc(value)
    text = f"{day.day} {_MONTHS[day.month - 1]} {day.year}"
    return f"{_WEEKDAYS[day.weekday()]} {text}" if weekday else text


def _money(currency: str, amount: Any) -> str:
    try:
        number = f"{Decimal(str(amount)):,.2f}"
    except InvalidOperation:
        return _esc(f"{currency} {amount}")
    symbol = _CURRENCY_SYMBOLS.get(currency)
    return _esc(f"{symbol}{number}" if symbol else f"{currency} {number}")


def _is_positive(amount: Any) -> bool:
    try:
        return Decimal(str(amount)) > 0
    except InvalidOperation:
        return False


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _fit_lines(text: str, font: str, max_size: float, min_size: float, width: float) -> tuple[float, list[str]]:
    """Return a font size and lines so a long unbroken value stays inside width."""
    size = max_size
    while size > min_size and stringWidth(text, font, size) > width:
        size -= 0.5
    lines: list[str] = []
    current = ""
    for char in text:
        if current and stringWidth(current + char, font, size) > width:
            lines.append(current)
            current = ""
        current += char
    lines.append(current)
    return size, lines


def _styles() -> dict[str, ParagraphStyle]:
    def style(name: str, **kwargs: Any) -> ParagraphStyle:
        base: dict[str, Any] = {"fontName": "Helvetica", "fontSize": 9, "leading": 12.5, "textColor": TEXT_DARK}
        base.update(kwargs)
        return ParagraphStyle(f"N07{name}", **base)

    return {
        "greeting": style("Greeting", fontName="Times-Bold", fontSize=14, leading=18, textColor=DARK_GREEN, spaceAfter=4),
        "body": style("Body", fontSize=9.5, leading=14),
        "small": style("Small", fontSize=8, leading=11, textColor=INK_MUTED),
        "h2": style("H2", fontName="Times-Bold", fontSize=14, leading=18, textColor=DARK_GREEN, keepWithNext=1),
        "card_title": style("CardTitle", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=MID_GREEN),
        "label": style("Label", fontSize=8, leading=11, textColor=INK_MUTED),
        "value": style("Value", fontSize=9, leading=12),
        "balance": style("Balance", fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=ALERT_RED),
        "th": style("TH", fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=colors.white),
        "td": style("TD", fontSize=8.5, leading=11.5),
        "note": style("Note", fontSize=8.5, leading=12, leftIndent=10, bulletIndent=0),
        "step_title": style("StepTitle", fontName="Helvetica-Bold", fontSize=9.5, leading=13, textColor=DARK_GREEN),
        "terms": style("Terms", fontSize=8, leading=11, textColor=INK_MUTED, leftIndent=6, rightIndent=6,
                       backColor=CREAM, borderColor=RULE_GREY, borderWidth=0.5, borderPadding=6),
        "banner_title": style("BannerTitle", fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=ALERT_RED, alignment=1),
        "banner_text": style("BannerText", fontSize=8.5, leading=11.5, textColor=ALERT_RED, alignment=1),
    }


def _section(title: str, s: dict[str, ParagraphStyle]) -> list[Any]:
    return [Paragraph(_esc(title), s["h2"]), HRFlowable(width="100%", thickness=0.8, color=GOLD, spaceBefore=1, spaceAfter=7)]


class _StepDot(Flowable):
    """Small numbered circle for the 'What happens next' list."""

    def __init__(self, number: int) -> None:
        super().__init__()
        self.number = number
        self.width = self.height = 20

    def wrap(self, avail_width: float, avail_height: float) -> tuple[float, float]:
        return self.width, self.height

    def draw(self) -> None:
        self.canv.setFillColor(DARK_GREEN)
        self.canv.circle(10, 10, 9, stroke=0, fill=1)
        self.canv.setFillColor(colors.white)
        self.canv.setFont("Helvetica-Bold", 9)
        self.canv.drawCentredString(10, 6.8, str(self.number))


def _test_banner(s: dict[str, ParagraphStyle]) -> list[Any]:
    table = Table(
        [[[Paragraph(_esc(TEST_BANNER_TITLE), s["banner_title"]),
           Paragraph(_esc(f"{TEST_LABEL}. This document was produced from a simulated test booking."), s["banner_text"])]]],
        colWidths=[_CONTENT_W],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEE2E2")),
        ("BOX", (0, 0), (-1, -1), 1.2, ALERT_RED),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return [table, Spacer(1, 12)]


def _payment_sentence(snapshot: "BookingConfirmationSnapshot") -> str:
    fin = snapshot.financials
    due = _long_date(fin.balance_due_date)
    if not _is_positive(fin.balance_due):
        return "Your booking is paid in full. Thank you."
    balance = _money(fin.currency, fin.balance_due)
    if _is_positive(fin.verified_paid_total):
        paid = _money(fin.currency, fin.verified_paid_total)
        return f"We have received your payment of {paid}. The remaining balance of <b>{balance}</b> is due by <b>{due}</b>."
    return f"The balance of <b>{balance}</b> is due by <b>{due}</b>."


def _welcome_block(snapshot: "BookingConfirmationSnapshot", s: dict[str, ParagraphStyle]) -> list[Any]:
    lead = snapshot.lead_guest
    stay = snapshot.stay
    try:
        nights = (dt.date.fromisoformat(stay.departure_date) - dt.date.fromisoformat(stay.arrival_date)).days
        stay_text = f"Your {nights}-night stay and {_plural(len(snapshot.golf_rounds), 'round')} of golf are set out below."
    except ValueError:
        stay_text = "Your stay and golf are set out below."
    intro = (
        f"Thank you for choosing {_esc(RESORT_NAME)}. We are delighted to confirm your Stay &amp; Play booking "
        f"and look forward to welcoming you on <b>{_long_date(stay.arrival_date)}</b>. {stay_text} "
        "Please check every detail and contact our Reservations team as soon as possible if anything needs correcting. "
        f"{_payment_sentence(snapshot)}"
    )
    if snapshot.booking_revision > 1:
        intro += f" This is revision {snapshot.booking_revision} of your booking and replaces any earlier confirmation."

    contact_cells = [
        [Paragraph("Lead guest", s["label"]), Paragraph("Email", s["label"]), Paragraph("Telephone", s["label"])],
        [
            Paragraph(f"<b>{_esc(lead.first_name)} {_esc(lead.last_name)}</b>", s["value"]),
            Paragraph(_esc(lead.email), s["value"]),
            Paragraph(_esc(lead.telephone or "Not provided"), s["value"]),
        ],
    ]
    strip = Table(contact_cells, colWidths=[_CONTENT_W * 0.34, _CONTENT_W * 0.40, _CONTENT_W * 0.26])
    strip.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALE_GREEN),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, MID_GREEN),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 1), (-1, 1), 1),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
    ]))
    return [
        Paragraph(f"Dear {_esc(lead.first_name)} {_esc(lead.last_name)},", s["greeting"]),
        Paragraph(intro, s["body"]),
        Spacer(1, 9),
        strip,
        Spacer(1, 14),
    ]


def _card(title: str, rows: list[tuple[str, Any]], width: float, s: dict[str, ParagraphStyle]) -> list[Any]:
    label_w = 82
    inner = Table(
        [[Paragraph(label, s["label"]), Paragraph(value, s["value"]) if isinstance(value, str) else value] for label, value in rows],
        colWidths=[label_w, width - 20 - label_w],
    )
    inner.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE_GREY),
    ]))
    return [Paragraph(_esc(title.upper()), s["card_title"]), Spacer(1, 3), inner]


def _room_type(room: Any) -> str:
    quantity = f" × {room.room_quantity} rooms" if room.room_quantity != 1 else ""
    return f"{_esc(room.room_category.replace('_', ' ').title())}{quantity}"


def _rooming_block(snapshot: "BookingConfirmationSnapshot", s: dict[str, ParagraphStyle]) -> list[Any]:
    rows: list[list[Any]] = [[Paragraph(label, s["th"]) for label in ("Room", "Type", "Guests")]]
    for index, room in enumerate(snapshot.stay.rooms, start=1):
        names = _esc(", ".join(room.occupant_names)) if room.occupant_names else "Names to be confirmed before arrival"
        rows.append([Paragraph(f"Room {index}", s["td"]), Paragraph(f"<b>{_room_type(room)}</b>", s["td"]), Paragraph(names, s["td"])])
    table = Table(rows, colWidths=[60, 150, _CONTENT_W - 210], repeatRows=1)
    table.setStyle(_table_style())
    return [CondPageBreak(_MIN_SECTION_SPACE), *_section("Rooms & Guests", s), table, Spacer(1, 16)]


def _summary_cards(snapshot: "BookingConfirmationSnapshot", s: dict[str, ParagraphStyle]) -> list[Any]:
    stay = snapshot.stay
    fin = snapshot.financials
    gap = 14
    card_w = (_CONTENT_W - gap) / 2

    try:
        nights_text = str((dt.date.fromisoformat(stay.departure_date) - dt.date.fromisoformat(stay.arrival_date)).days)
    except ValueError:
        nights_text = "–"
    stay_rows: list[tuple[str, Any]] = [
        ("Arrival", f"<b>{_long_date(stay.arrival_date)}</b>"),
        ("Departure", f"<b>{_long_date(stay.departure_date)}</b>"),
        ("Nights", nights_text),
    ]
    guest_total = sum(len(room.occupant_names) for room in stay.rooms)
    # A card row cannot split across pages, so large groups get a separate splittable rooming table.
    inline_names = len(stay.rooms) <= 3 and guest_total <= 8
    for index, room in enumerate(stay.rooms, start=1):
        label = "Room" if len(stay.rooms) == 1 else f"Room {index}"
        names = ""
        if inline_names and room.occupant_names:
            names = f"<br/><font color='#4B5563'>Guests: {_esc(', '.join(room.occupant_names))}</font>"
        stay_rows.append((label, f"<b>{_room_type(room)}</b>{names}"))
    stay_rows.append((
        "Guests",
        _plural(guest_total, "guest") if guest_total else "Names to be confirmed before arrival",
    ))

    pay_rows: list[tuple[str, Any]] = [
        ("Total price", f"<b>{_money(fin.currency, fin.total_amount)}</b>"),
        ("Deposit paid", _money(fin.currency, fin.deposit_paid)),
    ]
    if fin.verified_paid_total != fin.deposit_paid:
        pay_rows.append(("Total paid to date", _money(fin.currency, fin.verified_paid_total)))
    if _is_positive(fin.balance_due):
        pay_rows.append(("Balance due", Paragraph(_money(fin.currency, fin.balance_due), s["balance"])))
        pay_rows.append(("Balance due by", f"<b>{_long_date(fin.balance_due_date)}</b>"))
    else:
        pay_rows.append(("Balance due", f"<b>{_money(fin.currency, fin.balance_due)}</b> – paid in full"))
    pay_rows.append(("Currency", _esc(fin.currency)))

    outer = Table(
        [[_card("Your stay", stay_rows, card_w, s), "", _card("Payment summary", pay_rows, card_w, s)]],
        colWidths=[card_w, gap, card_w],
    )
    outer.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, 0), CREAM),
        ("BACKGROUND", (2, 0), (2, 0), CREAM),
        ("BOX", (0, 0), (0, 0), 0.5, RULE_GREY),
        ("BOX", (2, 0), (2, 0), 0.5, RULE_GREY),
        ("LINEABOVE", (0, 0), (0, 0), 2.5, DARK_GREEN),
        ("LINEABOVE", (2, 0), (2, 0), 2.5, DARK_GREEN),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (1, 0), (1, 0), 0),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
    ]))
    return [outer, Spacer(1, 16), *([] if inline_names else _rooming_block(snapshot, s))]


def _table_style() -> TableStyle:
    return TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), DARK_GREEN),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE_GREY),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE_GREY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE_GREEN]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ])


def _golf_block(snapshot: "BookingConfirmationSnapshot", s: dict[str, ParagraphStyle]) -> list[Any]:
    if not snapshot.golf_rounds:
        return []
    rows: list[list[Any]] = [[Paragraph(label, s["th"]) for label in ("Day", "Course", "Tee time", "Players")]]
    for rnd in sorted(snapshot.golf_rounds, key=lambda item: (item.date, item.tee_time)):
        try:
            weekday = _WEEKDAYS[dt.date.fromisoformat(rnd.date).weekday()]
            day_text = f"<b>{weekday}</b><br/>{_long_date(rnd.date, weekday=False)}"
        except ValueError:
            day_text = _esc(rnd.date)
        names = _esc(", ".join(rnd.golfer_names)) if rnd.golfer_names else "Names not yet supplied"
        rows.append([
            Paragraph(day_text, s["td"]),
            Paragraph(f"<b>{_esc(rnd.course_name)}</b><br/><font color='#4B5563'>{rnd.holes} holes</font>", s["td"]),
            Paragraph(f"<b>{_esc(rnd.tee_time)}</b><br/><font size='7' color='#4B5563'>{_esc(rnd.timezone)}</font>", s["td"]),
            Paragraph(f"<b>{_plural(rnd.players, 'player')}</b><br/><font color='#4B5563'>{names}</font>", s["td"]),
        ])
    widths = [82, 150, 66]
    table = Table(rows, colWidths=widths + [_CONTENT_W - sum(widths)], repeatRows=1)
    table.setStyle(_table_style())

    bullets = [
        "Please arrive at least <b>30 minutes before your tee time</b>.",
        "Your tee times are held under this booking reference. Please quote it when you arrive.",
        "Smart, traditional golf attire is required, and soft spikes only please.",
        "Caddies can be requested, subject to availability, and are paid for on the day. "
        "We will invite your requests in your pre-arrival email.",
    ]
    notes = Table([[[Paragraph(text, s["note"], bulletText="•") for text in bullets]]], colWidths=[_CONTENT_W])
    notes.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALE_GREEN),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, MID_GREEN),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return [CondPageBreak(_MIN_SECTION_SPACE), *_section("Your Golf Itinerary", s), table, Spacer(1, 6), notes, Spacer(1, 16)]


def _payments_block(snapshot: "BookingConfirmationSnapshot", s: dict[str, ParagraphStyle]) -> list[Any]:
    if not snapshot.payments:
        return []
    rows: list[list[Any]] = [[Paragraph(label, s["th"]) for label in ("Payment", "Amount", "Received", "Receipt reference")]]
    for payment in snapshot.payments:
        rows.append([
            Paragraph(_esc(_PAYMENT_PURPOSES.get(payment.payment_purpose, payment.payment_purpose.title())), s["td"]),
            Paragraph(f"<b>{_money(payment.currency, payment.amount)}</b>", s["td"]),
            Paragraph(_long_date(payment.verified_at, weekday=False), s["td"]),
            Paragraph(_esc(payment.receipt_reference or "–"), s["td"]),
        ])
    widths = [120, 100, 110]
    table = Table(rows, colWidths=widths + [_CONTENT_W - sum(widths)], repeatRows=1)
    table.setStyle(_table_style())
    return [KeepTogether([*_section("Payments Received", s), table]), Spacer(1, 16)]


def _next_steps_block(snapshot: "BookingConfirmationSnapshot", s: dict[str, ParagraphStyle]) -> list[Any]:
    fin = snapshot.financials
    if _is_positive(fin.balance_due):
        first = (
            "Balance reminder",
            f"We will email you a reminder before your balance of {_money(fin.currency, fin.balance_due)} "
            f"falls due on {_long_date(fin.balance_due_date)}.",
        )
    else:
        first = ("Payment complete", "Your balance is settled and no further payment is due.")
    steps = [
        first,
        (
            "Pre-arrival email",
            "Before you arrive we will email you so that you can confirm who is staying in each room "
            "(your rooming list) and send us any caddy requests. Caddy requests are subject to availability.",
        ),
        (
            "Arrival",
            f"We look forward to welcoming you on {_long_date(snapshot.stay.arrival_date)}. "
            "The hotel allocates room numbers shortly before you arrive. Please quote your booking reference at check-in.",
        ),
    ]
    rows = [
        [_StepDot(number), [Paragraph(_esc(title), s["step_title"]), Paragraph(text, s["body"])]]
        for number, (title, text) in enumerate(steps, start=1)
    ]
    table = Table(rows, colWidths=[30, _CONTENT_W - 30])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (1, 0), (1, -2), 0.4, RULE_GREY),
    ]))
    return [KeepTogether([*_section("What Happens Next", s), table]), Spacer(1, 16)]


def _terms_block(snapshot: "BookingConfirmationSnapshot", s: dict[str, ParagraphStyle]) -> list[Any]:
    version = _esc(snapshot.terms_version)
    policy = (snapshot.content.policy_text or "").strip()
    heading = _section("Terms & Conditions", s)
    version_line = Paragraph(f"Terms version: {version}", s["small"])
    if policy:
        body = Paragraph(_esc_lines(policy), s["terms"])
    else:
        # Placeholder only: the approved terms wording is not in the snapshot, so none is invented here.
        body = Paragraph(
            f"<b>Booking terms: see your reservation terms (version {version}).</b><br/>"
            "This document does not reproduce the full terms. Please contact our Reservations team if you would like a copy.",
            s["terms"],
        )
    return [
        CondPageBreak(_MIN_SECTION_SPACE),
        *heading,
        version_line,
        Spacer(1, 6),
        body,
        Spacer(1, 16),
    ]


def _contact_block(email: str, reference: str, s: dict[str, ParagraphStyle]) -> list[Any]:
    text = [
        Paragraph("Questions about your booking?", s["step_title"]),
        Paragraph(
            "Our Reservations team is happy to help with any question or change. "
            f"Email <b>{_esc(email)}</b> or telephone <b>{_esc(RESERVATIONS_PHONE)}</b>, "
            f"and please quote your booking reference <b>{_esc(reference)}</b>.",
            s["body"],
        ),
        Paragraph(_esc(f"{RESORT_NAME}, {RESORT_ADDRESS}"), s["small"]),
    ]
    box = Table([[text]], colWidths=[_CONTENT_W])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CREAM),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE_GREY),
        ("LINEBEFORE", (0, 0), (0, -1), 3, GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return [KeepTogether([box])]


def _draw_first_header(canv: Any, reference: str, revision: int, confirmed_on: str) -> None:
    top = _PAGE_H
    canv.saveState()
    canv.setFillColor(DARK_GREEN)
    canv.rect(0, top - _FIRST_HEADER_H, _PAGE_W, _FIRST_HEADER_H, stroke=0, fill=1)
    canv.setFillColor(GOLD)
    canv.rect(0, top - _FIRST_HEADER_H - 3, _PAGE_W, 3, stroke=0, fill=1)

    box_w, box_h = 188, 70
    box_x = _PAGE_W - _MARGIN - box_w
    left_w = box_x - _MARGIN - 16
    canv.setFillColor(GOLD)
    canv.setFont("Helvetica-Bold", 7.5)
    canv.drawString(_MARGIN, top - 32, "DOWNINGS  ·  SHEEPHAVEN BAY  ·  CO. DONEGAL")
    size, _ = _fit_lines(RESORT_NAME, "Times-Bold", 23, 15, left_w)
    canv.setFillColor(colors.white)
    canv.setFont("Times-Bold", size)
    canv.drawString(_MARGIN, top - 60, RESORT_NAME)
    canv.setFillColor(colors.HexColor("#E9D7A6"))
    canv.setFont("Helvetica", 12)
    canv.drawString(_MARGIN, top - 82, "Stay & Play Booking Confirmation")

    box_y = top - (_FIRST_HEADER_H + box_h) / 2
    canv.setFillColor(colors.HexColor("#27492F"))
    canv.setStrokeColor(GOLD)
    canv.setLineWidth(0.8)
    canv.roundRect(box_x, box_y, box_w, box_h, 5, stroke=1, fill=1)
    centre = box_x + box_w / 2
    canv.setFillColor(GOLD)
    canv.setFont("Helvetica-Bold", 7)
    canv.drawCentredString(centre, box_y + box_h - 16, "BOOKING REFERENCE")
    ref_size, ref_lines = _fit_lines(reference, "Helvetica-Bold", 15, 9, box_w - 16)
    canv.setFillColor(colors.white)
    canv.setFont("Helvetica-Bold", ref_size)
    line_y = box_y + box_h - 33 - (6 if len(ref_lines) == 1 else 0)
    for line in ref_lines[:3]:
        canv.drawCentredString(centre, line_y, line)
        line_y -= ref_size + 1
    canv.setFillColor(colors.HexColor("#E9D7A6"))
    canv.setFont("Helvetica", 7.5)
    canv.drawCentredString(centre, box_y + 9, f"Revision {revision}  ·  Confirmed {confirmed_on}")
    canv.restoreState()


def _draw_later_header(canv: Any, reference: str) -> None:
    canv.saveState()
    canv.setFillColor(DARK_GREEN)
    canv.rect(0, _PAGE_H - _LATER_HEADER_H, _PAGE_W, _LATER_HEADER_H, stroke=0, fill=1)
    canv.setFillColor(GOLD)
    canv.rect(0, _PAGE_H - _LATER_HEADER_H - 2, _PAGE_W, 2, stroke=0, fill=1)
    canv.setFillColor(colors.white)
    canv.setFont("Times-Bold", 12)
    canv.drawString(_MARGIN, _PAGE_H - 22, RESORT_NAME)
    size, _ = _fit_lines(reference, "Helvetica-Bold", 8.5, 6, _CONTENT_W - 230)
    canv.setFillColor(GOLD)
    canv.setFont("Helvetica-Bold", size)
    canv.drawRightString(_PAGE_W - _MARGIN, _PAGE_H - 21.5, f"Booking confirmation  ·  {reference}")
    canv.restoreState()


class _NumberedCanvas(Canvas):
    """Canvas that draws the footer (and test watermark) once the page count is known."""

    def __init__(self, *args: Any, footer: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._footer = footer
        self._saved_pages: list[dict[str, Any]] = []

    def showPage(self) -> None:
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved_pages)
        for number, state in enumerate(self._saved_pages, start=1):
            self.__dict__.update(state)
            self._draw_footer(number, total)
            super().showPage()
        super().save()

    def _draw_footer(self, number: int, total: int) -> None:
        info = self._footer
        if info["is_test"]:
            self.saveState()
            self.setFillColor(ALERT_RED)
            self.setFillAlpha(0.07)
            self.setFont("Helvetica-Bold", 130)
            self.translate(_PAGE_W / 2, _PAGE_H / 2 - 40)
            self.rotate(40)
            self.drawCentredString(0, 0, "TEST")
            self.restoreState()
        self.saveState()
        self.setStrokeColor(RULE_GREY)
        self.setLineWidth(0.6)
        self.line(_MARGIN, 58, _PAGE_W - _MARGIN, 58)
        self.setFillColor(TEXT_DARK)
        self.setFont("Helvetica-Bold", 8)
        self.drawString(_MARGIN, 46, f"Booking reference: {info['reference']}  ·  Revision {info['revision']}")
        self.drawRightString(_PAGE_W - _MARGIN, 46, f"Page {number} of {total}")
        self.setFillColor(INK_MUTED)
        self.setFont("Helvetica", 7)
        self.drawString(_MARGIN, 35, f"{RESORT_NAME}  ·  {RESORT_ADDRESS}  ·  {info['email']}")
        self.drawString(
            _MARGIN, 25,
            f"Please keep this confirmation for your records. It shows the booking details recorded on {info['confirmed_on']}.",
        )
        if info["is_test"]:
            self.setFillColor(ALERT_RED)
            self.setFont("Helvetica-Bold", 7.5)
            self.drawString(_MARGIN, 14, TEST_LABEL)
        self.restoreState()


def build_n07_confirmation_pdf(snapshot: BookingConfirmationSnapshot, *, reservations_email: str | None = None) -> bytes:
    """Render the guest booking-confirmation PDF (A4) for Ticket N07.

    Every booking fact comes from the saved snapshot; nothing is defaulted or guessed.

    Guarantees:
    - Escapes all snapshot strings before inserting them into ReportLab paragraphs.
    - Test snapshots carry a visible banner, a watermark and a footer label on every page.
    - Uses the exact validated amounts and currency from the snapshot.
    - Shows the saved policy text. If a real booking has none, shows a marked placeholder
      pointing to the terms version instead of inventing terms.
    - Caps PDF output size to 2 MiB.

    ``reservations_email`` is the configured reservations mailbox (for example the
    ``EMAIL_HOTEL_RESERVATIONS`` setting). It falls back to the resort default.
    """
    if snapshot.is_test and not (snapshot.content.policy_text or "").strip():
        raise ValueError("Approved policy text is required for a booking confirmation PDF")

    email = reservations_email if reservations_email and _MAILBOX.fullmatch(reservations_email) else DEFAULT_RESERVATIONS_EMAIL
    reference = snapshot.booking_reference
    revision = snapshot.booking_revision
    confirmed_on = _long_date(snapshot.confirmed_at, weekday=False)
    s = _styles()

    story: list[Any] = []
    if snapshot.is_test:
        story += _test_banner(s)
    story += _welcome_block(snapshot, s)
    story += _summary_cards(snapshot, s)
    story += _golf_block(snapshot, s)
    story += _payments_block(snapshot, s)
    story += _next_steps_block(snapshot, s)
    story += _terms_block(snapshot, s)
    story += _contact_block(email, reference, s)

    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        title=f"Booking Confirmation {reference}" + (" (TEST)" if snapshot.is_test else ""),
        author=RESORT_NAME,
        subject="Stay & Play booking confirmation",
        creator=RESORT_NAME,
    )
    frame_kwargs = {"leftPadding": 0, "rightPadding": 0, "topPadding": 0, "bottomPadding": 0}
    first_top = _PAGE_H - _FIRST_HEADER_H - 3 - 16
    later_top = _PAGE_H - _LATER_HEADER_H - 2 - 16
    doc.addPageTemplates([
        PageTemplate(
            "first",
            [Frame(_MARGIN, _FOOTER_H, _CONTENT_W, first_top - _FOOTER_H, id="first", **frame_kwargs)],
            onPage=lambda canv, _doc: _draw_first_header(canv, reference, revision, confirmed_on),
            autoNextPageTemplate="later",
        ),
        PageTemplate(
            "later",
            [Frame(_MARGIN, _FOOTER_H, _CONTENT_W, later_top - _FOOTER_H, id="later", **frame_kwargs)],
            onPage=lambda canv, _doc: _draw_later_header(canv, reference),
        ),
    ])
    footer = {
        "reference": reference,
        "revision": revision,
        "confirmed_on": confirmed_on,
        "email": email,
        "is_test": snapshot.is_test,
    }
    doc.build(story, canvasmaker=lambda *args, **kwargs: _NumberedCanvas(*args, footer=footer, **kwargs))
    pdf_bytes = buffer.getvalue()

    # Enforce 2 MiB limit from N07 plan
    if len(pdf_bytes) > 2 * 1024 * 1024:
        raise ValueError("Rendered PDF exceeds maximum allowed size of 2 MiB")

    return pdf_bytes
