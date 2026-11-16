"""PDF itinerary generator for Rosapenna Hotel & Golf Resort bookings."""

from __future__ import annotations

import datetime as dt
import html
import io
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from rosapenna_backend.api.schemas.communications import BookingConfirmationSnapshot

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
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


def build_n07_confirmation_pdf(snapshot: BookingConfirmationSnapshot) -> bytes:
    """Render an authoritative, strict PDF booking confirmation for Ticket N07.

    Guarantees:
    - Escapes all input strings before inserting into ReportLab Paragraphs.
    - Explicit 'TEST — NOT A REAL BOOKING CONFIRMATION' watermark/banner.
    - Uses exact validated financial amounts and currencies (no defaulting/guessing).
    - Caps PDF output size to 2 MiB.
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

    # Reusable styles
    title_style = ParagraphStyle(
        "N07DocTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=DARK_GREEN,
    )
    subtitle_style = ParagraphStyle(
        "N07DocSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=TEXT_MUTED,
    )
    banner_style = ParagraphStyle(
        "N07TestBanner",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#B91C1C"),
        alignment=1,  # Center
    )
    section_heading = ParagraphStyle(
        "N07SectionHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=DARK_GREEN,
        spaceAfter=6,
    )
    label_style = ParagraphStyle(
        "N07LabelStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=TEXT_DARK,
    )
    value_style = ParagraphStyle(
        "N07ValueStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=TEXT_DARK,
    )
    table_cell = ParagraphStyle(
        "N07TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=TEXT_DARK,
    )
    table_header = ParagraphStyle(
        "N07TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        textColor=colors.white,
    )
    footer_style = ParagraphStyle(
        "N07FooterStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=10,
        textColor=TEXT_MUTED,
        alignment=1,
    )

    elements = []

    # 1. Test Warning Banner
    banner_data = [[Paragraph("TEST — NOT A REAL BOOKING CONFIRMATION", banner_style)]]
    banner_table = Table(banner_data, colWidths=[540])
    banner_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEE2E2")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#EF4444")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(banner_table)
    elements.append(Spacer(1, 10))

    # 2. Header with resort branding
    ref = html.escape(snapshot.booking_reference)
    rev = snapshot.booking_revision
    header_data = [
        [
            Paragraph("ROSAPENNA HOTEL & GOLF RESORT", title_style),
            Paragraph(f"<b>BOOKING REFERENCE</b><br/><font size='12' color='#1A3323'><b>{ref}</b></font>", ParagraphStyle("N07Ref", alignment=2, fontName="Helvetica", leading=16)),
        ],
        [
            Paragraph("Downings, Sheephaven Bay, Co. Donegal, Ireland &bull; reservations@rosapenna.ie", subtitle_style),
            Paragraph(f"Revision: <b>#{rev}</b> &bull; Status: <b>Confirmed (Test)</b>", ParagraphStyle("N07Status", alignment=2, fontName="Helvetica", fontSize=8.5, textColor=DARK_GREEN)),
        ],
    ]
    header_table = Table(header_data, colWidths=[340, 200])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=GOLD, spaceAfter=12))

    # 3. Lead Guest Details
    lead = snapshot.lead_guest
    guest_name = html.escape(f"{lead.first_name} {lead.last_name}")
    guest_email = html.escape(lead.email)
    guest_phone = html.escape(lead.telephone or "Not provided")
    confirmed_date = html.escape(snapshot.confirmed_at[:10])

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
            Paragraph(confirmed_date, value_style),
        ],
    ]
    guest_table = Table(guest_data, colWidths=[80, 190, 95, 175])
    guest_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(guest_table)
    elements.append(Spacer(1, 12))

    # 4. Stay Accommodation
    elements.append(Paragraph("Accommodation Details", section_heading))
    stay_dates_text = f"<b>Arrival:</b> {html.escape(snapshot.stay.arrival_date)} &nbsp;&bull;&nbsp; <b>Departure:</b> {html.escape(snapshot.stay.departure_date)}"
    elements.append(Paragraph(stay_dates_text, table_cell))
    elements.append(Spacer(1, 6))

    room_rows = [
        [
            Paragraph("<b>Room Group</b>", table_header),
            Paragraph("<b>Category / Type</b>", table_header),
            Paragraph("<b>Quantity</b>", table_header),
            Paragraph("<b>Assigned Occupants</b>", table_header),
        ]
    ]
    for r in snapshot.stay.rooms:
        rg = html.escape(r.room_group_id)
        cat = html.escape(r.room_category.replace("_", " ").title())
        qty = str(r.room_quantity)
        occ = html.escape(", ".join(r.occupant_names) if r.occupant_names else "Names not supplied")
        room_rows.append([
            Paragraph(rg, table_cell),
            Paragraph(cat, table_cell),
            Paragraph(qty, table_cell),
            Paragraph(occ, table_cell),
        ])

    rooms_table = Table(room_rows, colWidths=[80, 180, 60, 220])
    rooms_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK_GREEN),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(rooms_table)
    elements.append(Spacer(1, 12))

    # 5. Championship Golf Rounds
    if snapshot.golf_rounds:
        elements.append(Paragraph("Championship Golf Itinerary", section_heading))
        golf_rows = [
            [
                Paragraph("<b>Date</b>", table_header),
                Paragraph("<b>Tee Time</b>", table_header),
                Paragraph("<b>Course</b>", table_header),
                Paragraph("<b>Players</b>", table_header),
                Paragraph("<b>Golfer Names</b>", table_header),
                Paragraph("<b>Holes</b>", table_header),
            ]
        ]
        for g in snapshot.golf_rounds:
            g_date = html.escape(g.date)
            g_time = html.escape(f"{g.tee_time} ({g.timezone})")
            g_course = html.escape(g.course_name)
            g_players = f"{g.players} golfer{'s' if g.players > 1 else ''}"
            g_names = html.escape(", ".join(g.golfer_names) if g.golfer_names else "Names not supplied")
            g_holes = f"{g.holes} Holes"
            golf_rows.append([
                Paragraph(g_date, table_cell),
                Paragraph(g_time, table_cell),
                Paragraph(g_course, table_cell),
                Paragraph(g_players, table_cell),
                Paragraph(g_names, table_cell),
                Paragraph(g_holes, table_cell),
            ])

        golf_table = Table(golf_rows, colWidths=[60, 85, 135, 55, 150, 55])
        golf_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), DARK_GREEN),
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(golf_table)
        elements.append(Spacer(1, 12))

    # 6. Financial Settlement & Balances
    elements.append(Paragraph("Payment & Balance Summary", section_heading))
    fin = snapshot.financials
    curr = html.escape(fin.currency)
    tot = html.escape(fin.total_amount)
    dep = html.escape(fin.deposit_paid)
    paid = html.escape(fin.verified_paid_total)
    bal = html.escape(fin.balance_due)
    due_date = html.escape(fin.balance_due_date)

    finance_data = [
        [
            Paragraph("Total Stay & Play Amount:", label_style),
            Paragraph(f"<b>{curr} {tot}</b>", value_style),
            Paragraph("Deposit Paid:", label_style),
            Paragraph(f"<b>{curr} {dep}</b>", value_style),
        ],
        [
            Paragraph("Total Verified Paid:", label_style),
            Paragraph(f"<b>{curr} {paid}</b>", value_style),
            Paragraph("Remaining Balance Due:", label_style),
            Paragraph(f"<font color='#B91C1C'><b>{curr} {bal}</b></font>", value_style),
        ],
        [
            Paragraph("Balance Due Date:", label_style),
            Paragraph(f"<b>{due_date}</b>", value_style),
            Paragraph("Currency:", label_style),
            Paragraph(f"<b>{curr}</b>", value_style),
        ],
    ]
    finance_table = Table(finance_data, colWidths=[170, 100, 150, 120])
    finance_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(finance_table)
    elements.append(Spacer(1, 14))

    if snapshot.payments:
        elements.append(Paragraph("Verified Payments", section_heading))
        payment_rows = [[
            Paragraph("<b>Purpose</b>", table_header),
            Paragraph("<b>Amount</b>", table_header),
            Paragraph("<b>Verified At</b>", table_header),
        ]]
        for payment in snapshot.payments:
            payment_rows.append([
                Paragraph(html.escape(payment.payment_purpose.title()), table_cell),
                Paragraph(f"{html.escape(payment.currency)} {html.escape(payment.amount)}", table_cell),
                Paragraph(html.escape(payment.verified_at), table_cell),
            ])
        elements.append(Table(payment_rows, colWidths=[150, 150, 240], repeatRows=1))
        elements.append(Spacer(1, 14))

    # 7. Terms and Policies
    elements.append(Paragraph("Resort Information & Terms", section_heading))
    policy_text = snapshot.content.policy_text
    if not policy_text or not policy_text.strip():
        raise ValueError("Approved policy text is required for a booking confirmation PDF")
    safe_policy = html.escape(policy_text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
    elements.append(Paragraph(safe_policy, table_cell))
    elements.append(Spacer(1, 10))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_COLOR, spaceAfter=6))
    elements.append(Paragraph("Rosapenna Hotel & Golf Resort &bull; Official Itinerary Document &bull; Generated Securely (N07 Structure Preview)", footer_style))

    doc.build(elements)
    buffer.seek(0)
    pdf_bytes = buffer.getvalue()

    # Enforce 2 MiB limit from N07 plan
    if len(pdf_bytes) > 2 * 1024 * 1024:
        raise ValueError("Rendered PDF exceeds maximum allowed size of 2 MiB")

    return pdf_bytes
