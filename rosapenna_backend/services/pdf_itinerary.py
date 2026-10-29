"""PDF itinerary generator for Rosapenna Hotel & Golf Resort bookings."""

from __future__ import annotations

import datetime as dt
import io
from typing import Any, Dict

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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
    table_cell_bold = ParagraphStyle(
        "TableCellBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
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
    header_data = [
        [
            Paragraph("ROSAPENNA HOTEL & GOLF RESORT", title_style),
            Paragraph(f"<b>BOOKING REFERENCE</b><br/><font size='12' color='#1A3323'><b>{ref}</b></font>", ParagraphStyle("Ref", alignment=2, fontName="Helvetica", leading=16)),
        ],
        [
            Paragraph("Downings, Sheephaven Bay, Co. Donegal, Ireland · reservations@rosapenna.ie", subtitle_style),
            Paragraph(f"Status: <b>{booking.get('stage', 'Confirmed').replace('_', ' ').title()}</b>", ParagraphStyle("Status", alignment=2, fontName="Helvetica", fontSize=9, textColor=DARK_GREEN)),
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
    guest_email = lead.get("email", "—")
    guest_phone = lead.get("telephone", "—")

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

    # 3. Accommodation Details (Opera Cloud)
    opera_res = booking.get("links", {}).get("opera_reservation")
    elements.append(Paragraph("Accommodation Details", section_heading))

    if opera_res:
        arr_date = opera_res.get("arrival_date", "—")
        dep_date = opera_res.get("departure_date", "—")
        room_cat = opera_res.get("room_category", "Standard Room").replace("_", " ").title()
        room_qty = opera_res.get("room_quantity", 1)
        conf_num = opera_res.get("confirmation_number") or opera_res.get("reservation_id", "—")

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
        elements.append(Paragraph("<font size='7.5' color='#64748B'>* Note: Hotel allocates physical room numbers approximately 2 days prior to arrival.</font>", subtitle_style))
    else:
        elements.append(Paragraph("No hotel accommodation linked to this booking.", table_cell))

    elements.append(Spacer(1, 14))

    # 4. Championship Golf Rounds (BRS Golf)
    brs_rounds = booking.get("links", {}).get("brs_rounds", [])
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
            if cad_req and cad_req.get("caddies_requested", 0) > 0:
                caddy_info = f"{cad_req['caddies_requested']} {cad_req.get('caddy_category', 'caddie').title()}(s)"

            golf_data.append([
                Paragraph(f"<b>{r.get('course_name', 'Championship Links')}</b>", table_cell),
                Paragraph(str(r.get("date", "—")), table_cell),
                Paragraph(str(r.get("time", "—")), table_cell),
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
        elements.append(Paragraph("<font size='7.5' color='#64748B'>* Note: Caddies are availability requests payable directly to the caddie master on the day of play (€90 per bag).</font>", subtitle_style))
    else:
        elements.append(Paragraph("No golf rounds linked to this booking.", table_cell))

    elements.append(Spacer(1, 14))

    # 5. Financial Summary & Verified Ledger (F05)
    elements.append(Paragraph("Payment & Financial Ledger", section_heading))

    # Calculate total and paid amount
    total_val = "0.00"
    if "quote" in booking and "selected" in booking["quote"]:
        total_val = str(booking["quote"]["selected"].get("amount") or booking["quote"]["selected"].get("price", "0.00"))
    elif "selection" in booking:
        total_val = str(booking["selection"].get("price", "0.00"))

    payments = booking.get("links", {}).get("payments", [])
    verified_paid = sum(
        (float(p.get("amount", 0.0)) for p in payments if p.get("status") == "completed"),
        0.0,
    )
    total_num = float(total_val)
    balance_due = max(0.0, total_num - verified_paid)

    finance_data = [
        [
            Paragraph("Total Package Price (inc. VAT):", label_style),
            Paragraph(f"<b>€{total_num:,.2f}</b>", value_style),
            Paragraph("Verified Paid to Date:", label_style),
            Paragraph(f"<b>€{verified_paid:,.2f}</b>", value_style),
        ],
        [
            Paragraph("Remaining Balance Due:", label_style),
            Paragraph(f"<font color='#B91C1C'><b>€{balance_due:,.2f}</b></font>", value_style),
            Paragraph("Balance Due Date:", label_style),
            Paragraph("6 Weeks Prior to Arrival", value_style),
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
        "The remaining 75% balance is due 6 weeks prior to arrival.<br/>"
        "<b>Golf Dress Code:</b> Traditional golf attire is required. Soft spikes only on all three links courses.<br/>"
        "<b>Need Assistance?</b> Contact Reservations at reservations@rosapenna.ie or +353 (0)74 915 5301."
    )
    elements.append(Paragraph(policy_text, table_cell))
    elements.append(Spacer(1, 15))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_COLOR, spaceAfter=8))
    elements.append(Paragraph("Rosapenna Hotel & Golf Resort · Official Itinerary Document · Generated Securely", footer_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()
