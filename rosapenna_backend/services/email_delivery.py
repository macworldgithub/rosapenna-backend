"""Email and itinerary delivery service supporting SMTP, sandbox simulation, and PDF attachments."""

from __future__ import annotations

import datetime as dt
import email.utils
import smtplib
import uuid
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from rosapenna_backend.api.schemas.communications import EmailDeliveryResult
from rosapenna_backend.core.logging import logger
from rosapenna_backend.services.pdf_itinerary import build_itinerary_pdf


class SmtpSettings(BaseSettings):
    """Configuration for SMTP email delivery."""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    smtp_host: str = Field("smtp.hostinger.com", validation_alias="SMTP_HOST")
    smtp_port: int = Field(465, validation_alias="SMTP_PORT")
    smtp_secure: bool = Field(True, validation_alias="SMTP_SECURE")
    smtp_user: str = Field("", validation_alias="SMTP_USER")
    smtp_pass: SecretStr = Field(SecretStr(""), validation_alias="SMTP_PASS")
    from_email: str = Field("noreply@neximet.com", validation_alias="EMAIL_FROM")
    from_name: str = Field("Rosapenna Hotel & Golf Resort", validation_alias="EMAIL_FROM_NAME")
    hotel_reservations_email: str = Field("reservations@rosapenna.ie", validation_alias="EMAIL_HOTEL_RESERVATIONS")
    allow_live_send: bool = Field(False, validation_alias="EMAIL_ALLOW_LIVE_SEND")
    mmb_enabled: bool = Field(False, validation_alias="MMB_ENABLED")


class EmailDeliveryService:
    """Manages email templates, PDF attachment compilation, and SMTP delivery."""

    def __init__(self, settings: Optional[SmtpSettings] = None):
        self.settings = settings or SmtpSettings()
        # In-memory sandbox log for testing/inspection
        self._dispatched_emails: List[Dict[str, Any]] = []

    def send_booking_confirmation(
        self,
        booking: Dict[str, Any],
        recipient_override: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> EmailDeliveryResult:
        """Send formal booking confirmation and rendered PDF itinerary.

        Dispatches to lead guest and notifies reservations@rosapenna.ie.
        """
        lead = booking.get("lead_guest") or {}
        recipient = recipient_override or lead.get("email")
        if not recipient:
            return EmailDeliveryResult(
                success=False,
                delivery_id=str(uuid.uuid4()),
                template="booking_confirmation",
                recipient="unknown",
                subject="Booking Confirmation",
                status="failed",
                sent_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                error_message="Recipient email address is missing.",
            )

        ref = booking.get("booking_reference") or booking.get("booking_intent_id", "REF-PENDING")
        guest_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Guest"
        subject = f"Booking Confirmation & Itinerary — Rosapenna Hotel & Golf Resort ({ref})"

        # 1. Render PDF Itinerary
        pdf_bytes = build_itinerary_pdf(booking)

        # 2. Build email body
        mmb_section = (
            f"<p><b>Manage Your Booking:</b> You can view your itinerary online at "
            f"<a href='https://rosapenna.ie/manage-booking?ref={ref}'>rosapenna.ie/manage-booking</a>.</p>"
            if self.settings.mmb_enabled
            else "<p><b>Manage Your Booking:</b> Online self-service is currently in preparation. "
                 "To adjust your stay, contact Reservations at reservations@rosapenna.ie or call +353 (0)74 915 5301.</p>"
        )

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1a202c; max-width: 650px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #1A3323; padding: 20px; text-align: center; border-bottom: 3px solid #DBB155;">
                <h1 style="color: #ffffff; margin: 0; font-size: 22px; letter-spacing: 1px;">ROSAPENNA HOTEL & GOLF RESORT</h1>
                <p style="color: #DBB155; margin: 5px 0 0 0; font-size: 13px;">Downings, Sheephaven Bay, Co. Donegal, Ireland</p>
            </div>
            <div style="padding: 25px 20px; border: 1px solid #e2e8f0; border-top: none;">
                <p>Dear {guest_name},</p>
                <p>We are delighted to confirm your stay and play reservation with Rosapenna Hotel & Golf Resort.</p>
                <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; padding: 15px; border-radius: 4px; margin: 20px 0;">
                    <p style="margin: 0 0 8px 0;"><b>Booking Reference:</b> <span style="color: #1A3323; font-size: 16px;">{ref}</span></p>
                    <p style="margin: 0;"><b>Status:</b> Confirmed & Guaranteed</p>
                </div>
                <p>Your complete itinerary, including hotel room allocation, championship links tee times, and verified payment ledger, is attached as a PDF document to this email.</p>
                {mmb_section}
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 25px 0;" />
                <p style="font-size: 13px; color: #64748b;">
                    If you have any questions or special requests, please reply to this email or contact us at <a href="mailto:reservations@rosapenna.ie">reservations@rosapenna.ie</a>.<br/>
                    We look forward to welcoming you to the magnificent links of Donegal.
                </p>
            </div>
        </body>
        </html>
        """

        plain_body = f"""ROSAPENNA HOTEL & GOLF RESORT
Booking Reference: {ref}
Status: Confirmed

Dear {guest_name},

Thank you for booking your Stay & Play package with Rosapenna Hotel & Golf Resort.
Your official itinerary is attached as a PDF to this email.

For any questions, please contact reservations@rosapenna.ie or call +353 (0)74 915 5301.

Warm regards,
Reservations Team
Rosapenna Hotel & Golf Resort
"""

        # 3. Dispatch or simulate
        delivery_id = f"DEL-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()

        if not self.settings.allow_live_send:
            logger.info("Email live send is disabled by policy (EMAIL_ALLOW_LIVE_SEND=false); using simulated sandbox delivery.")
            self._dispatched_emails.append({
                "delivery_id": delivery_id,
                "template": "booking_confirmation",
                "recipient": recipient,
                "hotel_copy": self.settings.hotel_reservations_email,
                "subject": subject,
                "pdf_size": len(pdf_bytes),
                "dispatched_at": now_str,
            })
            return EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="booking_confirmation",
                recipient=recipient,
                hotel_notified=True,
                pdf_attached=True,
                subject=subject,
                status="simulated_sandbox",
                sent_at=now_str,
            )

        # 4. Live SMTP dispatch
        try:
            self._send_smtp_email(
                recipient=recipient,
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
                pdf_attachment=pdf_bytes,
                pdf_filename=f"Rosapenna-Itinerary-{ref}.pdf",
                cc_recipient=self.settings.hotel_reservations_email,
            )
            return EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="booking_confirmation",
                recipient=recipient,
                hotel_notified=True,
                pdf_attached=True,
                subject=subject,
                status="sent",
                sent_at=now_str,
            )
        except Exception as exc:
            logger.error("Failed to send booking confirmation via SMTP: %s", exc)
            return EmailDeliveryResult(
                success=False,
                delivery_id=delivery_id,
                template="booking_confirmation",
                recipient=recipient,
                hotel_notified=False,
                pdf_attached=True,
                subject=subject,
                status="failed",
                sent_at=now_str,
                error_message=str(exc),
            )

    def send_special_request_acknowledgement(
        self,
        booking: Dict[str, Any],
        request_text: str,
        category: str = "other",
    ) -> EmailDeliveryResult:
        """Acknowledge guest special request as pending staff review.

        Crucially, does not mutate room or golf inventory or payments.
        """
        lead = booking.get("lead_guest") or {}
        recipient = lead.get("email") or "guest@example.com"
        ref = booking.get("booking_reference") or booking.get("booking_intent_id", "REF-PENDING")
        guest_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Valued Guest"
        subject = f"Special Request Received — Booking {ref}"
        delivery_id = f"DEL-REQ-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1a202c; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #1A3323; padding: 15px; text-align: center; border-bottom: 3px solid #DBB155;">
                <h2 style="color: #ffffff; margin: 0; font-size: 18px;">ROSAPENNA HOTEL & GOLF RESORT</h2>
            </div>
            <div style="padding: 20px; border: 1px solid #e2e8f0; border-top: none;">
                <p>Dear {guest_name},</p>
                <p>We have received your special request regarding booking <b>{ref}</b>:</p>
                <blockquote style="background-color: #f8fafc; border-left: 4px solid #DBB155; padding: 12px; margin: 15px 0; color: #334155;">
                    <i>"{request_text}"</i>
                </blockquote>
                <p>Your request has been forwarded to our Reservations and Concierge team and is currently <b>pending staff review</b>. Please note that special requests are subject to resort availability and do not modify your confirmed stay dates or golf tee times.</p>
                <p>Our team will follow up directly if further arrangements or verification are required.</p>
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;" />
                <p style="font-size: 12px; color: #64748b;">Rosapenna Reservations Team · reservations@rosapenna.ie</p>
            </div>
        </body>
        </html>
        """
        plain_body = f"""Dear {guest_name},

We have received your special request regarding booking {ref}:
"{request_text}"

Your request is pending staff review. Confirmed arrangements remain unchanged.
Reservations Team · Rosapenna Hotel & Golf Resort
"""

        if not self.settings.allow_live_send:
            logger.info("Simulating special request acknowledgement dispatch.")
            self._dispatched_emails.append({
                "delivery_id": delivery_id,
                "template": "special_request_acknowledgement",
                "recipient": recipient,
                "hotel_copy": self.settings.hotel_reservations_email,
                "subject": subject,
                "request_text": request_text,
                "dispatched_at": now_str,
            })
            return EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="special_request_acknowledgement",
                recipient=recipient,
                hotel_notified=True,
                pdf_attached=False,
                subject=subject,
                status="simulated_sandbox",
                sent_at=now_str,
            )

        try:
            self._send_smtp_email(
                recipient=recipient,
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
                cc_recipient=self.settings.hotel_reservations_email,
            )
            return EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="special_request_acknowledgement",
                recipient=recipient,
                hotel_notified=True,
                pdf_attached=False,
                subject=subject,
                status="sent",
                sent_at=now_str,
            )
        except Exception as exc:
            return EmailDeliveryResult(
                success=False,
                delivery_id=delivery_id,
                template="special_request_acknowledgement",
                recipient=recipient,
                subject=subject,
                status="failed",
                sent_at=now_str,
                error_message=str(exc),
            )

    def send_one_time_code(
        self,
        recipient_email: str,
        recipient_name: str,
        code: str,
        booking_reference: str,
    ) -> EmailDeliveryResult:
        """Send 6-digit one-time verification code for Manage My Booking access.

        Ensures code is never exposed in logging or URL query strings.
        """
        subject = f"Your Verification Code — Rosapenna Manage My Booking ({booking_reference})"
        delivery_id = f"DEL-OTP-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1a202c; max-width: 500px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #1A3323; padding: 15px; text-align: center; border-bottom: 3px solid #DBB155;">
                <h3 style="color: #ffffff; margin: 0;">ROSAPENNA HOTEL & GOLF RESORT</h3>
            </div>
            <div style="padding: 20px; border: 1px solid #e2e8f0; border-top: none; text-align: center;">
                <p>Hello {recipient_name},</p>
                <p>Use the verification code below to access your booking <b>{booking_reference}</b>:</p>
                <div style="background-color: #f1f5f9; border: 1px dashed #94a3b8; font-size: 28px; font-weight: bold; letter-spacing: 6px; padding: 15px; margin: 20px 0; color: #1A3323;">
                    {code}
                </div>
                <p style="font-size: 13px; color: #64748b;">This code is valid for 10 minutes. Do not share this code with anyone.</p>
            </div>
        </body>
        </html>
        """
        plain_body = f"Hello {recipient_name},\nYour Rosapenna verification code for booking {booking_reference} is: {code}\nValid for 10 minutes."

        # Redact code from routine logs
        logger.info("Dispatching one-time-code verification email to %s (code redacted)", recipient_email)

        if not self.settings.allow_live_send:
            self._dispatched_emails.append({
                "delivery_id": delivery_id,
                "template": "one_time_code",
                "recipient": recipient_email,
                "subject": subject,
                "dispatched_at": now_str,
            })
            return EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="one_time_code",
                recipient=recipient_email,
                hotel_notified=False,
                pdf_attached=False,
                subject=subject,
                status="simulated_sandbox",
                sent_at=now_str,
            )

        try:
            self._send_smtp_email(
                recipient=recipient_email,
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
            )
            return EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="one_time_code",
                recipient=recipient_email,
                hotel_notified=False,
                pdf_attached=False,
                subject=subject,
                status="sent",
                sent_at=now_str,
            )
        except Exception as exc:
            return EmailDeliveryResult(
                success=False,
                delivery_id=delivery_id,
                template="one_time_code",
                recipient=recipient_email,
                subject=subject,
                status="failed",
                sent_at=now_str,
                error_message=str(exc),
            )

    def send_balance_reminder(
        self,
        booking: Dict[str, Any],
        recipient_override: Optional[str] = None,
    ) -> EmailDeliveryResult:
        """Send reminder email for outstanding balance 6 weeks before arrival."""
        lead = booking.get("lead_guest") or {}
        recipient = recipient_override or lead.get("email")
        if not recipient:
            return EmailDeliveryResult(
                success=False,
                delivery_id=str(uuid.uuid4()),
                template="balance_reminder",
                recipient="unknown",
                subject="Balance Reminder",
                status="failed",
                sent_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                error_message="Recipient email address is missing.",
            )

        ref = booking.get("booking_reference") or booking.get("booking_intent_id", "REF-PENDING")
        guest_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Valued Guest"
        subject = f"Upcoming Stay & Balance Reminder — Rosapenna Hotel & Golf Resort ({ref})"

        quote = booking.get("quote", {})
        total_price = float((quote.get("live_revalidated") or quote.get("selected") or {}).get("amount", 0.0))
        currency = (quote.get("live_revalidated") or quote.get("selected") or {}).get("currency", "EUR")

        payments = booking.get("links", {}).get("payments", [])
        paid_amount = sum(
            float(p.get("amount", 0.0))
            for p in payments
            if p.get("status") in {"completed", "captured", "paid"}
        )
        balance_due = max(0.0, total_price - paid_amount)

        selection = booking.get("selection") or {}
        date_str = selection.get("date")
        balance_due_date_str = "6 weeks before arrival"
        if date_str:
            try:
                arrival_date = dt.date.fromisoformat(str(date_str).split("T")[0])
                due_date = arrival_date - dt.timedelta(weeks=6)
                balance_due_date_str = due_date.strftime("%d %B %Y")
            except Exception:
                pass

        delivery_id = f"DEL-BAL-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()

        mmb_payment_info = (
            f"<p>To settle your balance securely online, please visit "
            f"<a href='https://rosapenna.ie/manage-booking?ref={ref}'>Manage My Booking</a>.</p>"
            if self.settings.mmb_enabled
            else "<p>To settle your remaining balance, please contact our Reservations team at "
                 "<a href='mailto:reservations@rosapenna.ie'>reservations@rosapenna.ie</a> or call +353 (0)74 915 5301.</p>"
        )

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1a202c; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #1A3323; padding: 20px; text-align: center; border-bottom: 3px solid #DBB155;">
                <h2 style="color: #ffffff; margin: 0;">ROSAPENNA HOTEL & GOLF RESORT</h2>
                <p style="color: #DBB155; margin: 5px 0 0 0; font-size: 13px;">Upcoming Stay & Balance Notification</p>
            </div>
            <div style="padding: 25px 20px; border: 1px solid #e2e8f0; border-top: none;">
                <p>Dear {guest_name},</p>
                <p>We are eagerly anticipating your upcoming visit to Rosapenna Hotel & Golf Resort.</p>
                <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; padding: 15px; border-radius: 4px; margin: 20px 0;">
                    <p style="margin: 0 0 5px 0;"><b>Booking Reference:</b> {ref}</p>
                    <p style="margin: 0 0 5px 0;"><b>Total Package Price:</b> {currency} {total_price:,.2f}</p>
                    <p style="margin: 0 0 5px 0;"><b>Verified Paid:</b> {currency} {paid_amount:,.2f}</p>
                    <p style="margin: 0 0 5px 0; color: #b91c1c; font-weight: bold;"><b>Remaining Balance Due:</b> {currency} {balance_due:,.2f}</p>
                    <p style="margin: 0; font-size: 13px; color: #64748b;"><b>Due Date:</b> {balance_due_date_str}</p>
                </div>
                {mmb_payment_info}
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 25px 0;" />
                <p style="font-size: 12px; color: #64748b;">
                    Reservations Team · Rosapenna Hotel & Golf Resort · reservations@rosapenna.ie
                </p>
            </div>
        </body>
        </html>
        """
        plain_body = f"""ROSAPENNA HOTEL & GOLF RESORT
Upcoming Stay & Balance Notification
Booking Reference: {ref}
Dear {guest_name},
Remaining Balance Due: {currency} {balance_due:,.2f}
Due Date: {balance_due_date_str}

Please contact reservations@rosapenna.ie or call +353 (0)74 915 5301.
"""

        if not self.settings.allow_live_send:
            self._dispatched_emails.append({
                "delivery_id": delivery_id,
                "template": "balance_reminder",
                "recipient": recipient,
                "subject": subject,
                "dispatched_at": now_str,
            })
            return EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="balance_reminder",
                recipient=recipient,
                hotel_notified=False,
                pdf_attached=False,
                subject=subject,
                status="simulated_sandbox",
                sent_at=now_str,
            )

        try:
            self._send_smtp_email(
                recipient=recipient,
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
            )
            return EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="balance_reminder",
                recipient=recipient,
                hotel_notified=False,
                pdf_attached=False,
                subject=subject,
                status="sent",
                sent_at=now_str,
            )
        except Exception as exc:
            return EmailDeliveryResult(
                success=False,
                delivery_id=delivery_id,
                template="balance_reminder",
                recipient=recipient,
                subject=subject,
                status="failed",
                sent_at=now_str,
                error_message=str(exc),
            )

    def _send_smtp_email(
        self,
        recipient: str,
        subject: str,
        html_body: str,
        plain_body: str,
        pdf_attachment: Optional[bytes] = None,
        pdf_filename: str = "Itinerary.pdf",
        cc_recipient: Optional[str] = None,
    ) -> None:
        """Connect to configured SMTP server with TLS/SSL and dispatch MIME message."""
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = email.utils.formataddr((self.settings.from_name, self.settings.from_email))
        msg["To"] = recipient
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain="rosapenna.ie")

        recipients = [recipient]
        if cc_recipient:
            msg["Cc"] = cc_recipient
            recipients.append(cc_recipient)

        # Body multipart/alternative
        alt_part = MIMEMultipart("alternative")
        alt_part.attach(MIMEText(plain_body, "plain", "utf-8"))
        alt_part.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt_part)

        # Attach PDF if present
        if pdf_attachment:
            pdf_part = MIMEApplication(pdf_attachment, _subtype="pdf")
            pdf_part.add_header("Content-Disposition", "attachment", filename=pdf_filename)
            msg.attach(pdf_part)

        password = self.settings.smtp_pass.get_secret_value()

        if self.settings.smtp_port == 465 or self.settings.smtp_secure:
            with smtplib.SMTP_SSL(self.settings.smtp_host, self.settings.smtp_port, timeout=15) as server:
                if self.settings.smtp_user and password:
                    server.login(self.settings.smtp_user, password)
                server.send_message(msg, from_addr=self.settings.from_email, to_addrs=recipients)
        else:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=15) as server:
                server.starttls()
                if self.settings.smtp_user and password:
                    server.login(self.settings.smtp_user, password)
                server.send_message(msg, from_addr=self.settings.from_email, to_addrs=recipients)
