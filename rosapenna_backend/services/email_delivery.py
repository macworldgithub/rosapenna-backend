"""Email and itinerary delivery service supporting SMTP, sandbox simulation, and PDF attachments."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
import email.utils
import hashlib
from pathlib import Path
import re
import smtplib
import ssl
import uuid
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import html
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
    from rosapenna_backend.api.schemas.communications import (
        BookingConfirmationSnapshot,
        ConfirmationDeliveryState,
    )
    from rosapenna_backend.api.schemas.pre_arrival import (
        EligibleBookingInput,
        PreArrivalPolicyDoc,
    )

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from rosapenna_backend.core.settings_env import ENV_FILE

from rosapenna_backend.api.schemas.communications import EmailDeliveryResult
from rosapenna_backend.core.logging import logger
from rosapenna_backend.services.mmb_gate import MmbGateService
from rosapenna_backend.services.pdf_itinerary import build_itinerary_pdf


class SmtpSettings(BaseSettings):
    """Configuration for SMTP email delivery."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
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
    mmb_test_otp_recipient: str = Field("", validation_alias="EMAIL_MMB_TEST_OTP_RECIPIENT")


class EmailDeliveryService:
    """Manages email templates, PDF attachment compilation, and SMTP delivery."""

    def __init__(self, settings: Optional[SmtpSettings] = None, *, mmb_gate: Optional[MmbGateService] = None):
        self.settings = settings or SmtpSettings()
        self.mmb_gate = mmb_gate or MmbGateService()
        # In-memory sandbox log for testing/inspection
        self._dispatched_emails: List[Dict[str, Any]] = []
        self._idempotency_records: Dict[str, EmailDeliveryResult] = {}

    def send_booking_confirmation(
        self,
        booking: Dict[str, Any],
        recipient_override: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> EmailDeliveryResult:
        """Send formal booking confirmation and rendered PDF itinerary.

        Dispatches to lead guest and notifies reservations@rosapenna.ie.
        Deduplicates repeat dispatches when idempotency_key is provided.
        """
        if idempotency_key and idempotency_key in self._idempotency_records:
            logger.info("Returning replayed email delivery for idempotency_key %s", idempotency_key)
            replayed = self._idempotency_records[idempotency_key].model_copy()
            replayed.idempotency_replayed = True
            return replayed

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
        safe_guest_name = html.escape(guest_name)
        safe_ref = html.escape(str(ref))
        subject = f"Booking Confirmation & Itinerary — Rosapenna Hotel & Golf Resort ({ref})"

        # 1. Render PDF Itinerary
        pdf_bytes = build_itinerary_pdf(booking)

        # 2. Build email body
        mmb_section = (
            f"<p><b>Manage Your Booking:</b> You can view your itinerary online at "
            f"<a href='https://rosapenna.ie/manage-booking?ref={quote(str(ref), safe='')}'>rosapenna.ie/manage-booking</a>.</p>"
            if self.mmb_gate.is_enabled()
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
                <p>Dear {safe_guest_name},</p>
                <p>We are delighted to confirm your stay and play reservation with Rosapenna Hotel & Golf Resort.</p>
                <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; padding: 15px; border-radius: 4px; margin: 20px 0;">
                    <p style="margin: 0 0 8px 0;"><b>Booking Reference:</b> <span style="color: #1A3323; font-size: 16px;">{safe_ref}</span></p>
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
            res = EmailDeliveryResult(
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
            if idempotency_key:
                self._idempotency_records[idempotency_key] = res
            return res

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
            res = EmailDeliveryResult(
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
            if idempotency_key:
                self._idempotency_records[idempotency_key] = res
            return res
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

    def send_revised_booking_confirmation(
        self,
        booking: Dict[str, Any],
        recipient_override: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        revision_number: Optional[int] = None,
        amendment_summary: Optional[str] = None,
    ) -> EmailDeliveryResult:
        """Send official revised booking confirmation and updated PDF itinerary (Ticket M15).

        Dispatched only after verified vendor writes and readback.
        Deduplicates repeat dispatches when idempotency_key is provided.
        Dual-notifies lead guest and reservations@rosapenna.ie.
        """
        if idempotency_key and idempotency_key in self._idempotency_records:
            logger.info("Returning replayed email delivery for idempotency_key %s", idempotency_key)
            replayed = self._idempotency_records[idempotency_key].model_copy()
            replayed.idempotency_replayed = True
            return replayed

        lead = booking.get("lead_guest") or {}
        recipient = recipient_override or lead.get("email")
        if not recipient:
            return EmailDeliveryResult(
                success=False,
                delivery_id=str(uuid.uuid4()),
                template="revised_booking_confirmation",
                recipient="unknown",
                subject="Revised Booking Confirmation",
                status="failed",
                sent_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                error_message="Recipient email address is missing.",
            )

        ref = booking.get("booking_reference") or booking.get("booking_intent_id", "REF-PENDING")
        rev = revision_number or booking.get("revision_number") or booking.get("booking_revision", 1)
        guest_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Valued Guest"
        safe_guest_name = html.escape(guest_name)
        safe_ref = html.escape(str(ref))
        safe_summary = html.escape(amendment_summary) if amendment_summary else "Your booking arrangements have been successfully updated."

        subject = f"Revised Booking Confirmation & Updated Itinerary – Rosapenna Hotel & Golf Resort ({ref} - Rev #{rev})"

        # 1. Render updated authoritative PDF Itinerary matching latest revision
        booking_for_pdf = dict(booking)
        booking_for_pdf["revision_number"] = rev
        pdf_bytes = build_itinerary_pdf(booking_for_pdf)

        # 2. Build email body
        mmb_section = (
            f"<p><b>Manage Your Booking:</b> You can view your updated itinerary online at "
            f"<a href='https://rosapenna.ie/manage-booking?ref={quote(str(ref), safe='')}'>rosapenna.ie/manage-booking</a>.</p>"
            if self.mmb_gate.is_enabled()
            else "<p><b>Manage Your Booking:</b> Online self-service is currently in preparation. "
                 "To adjust your stay, contact Reservations at reservations@rosapenna.ie or call +353 (0)74 915 5301.</p>"
        )

        fin = booking.get("financials") or {}
        balance_due_str = str(fin.get("balance_due", "0.00"))
        due_date_str = str(fin.get("balance_due_date") or "Strictly 6 weeks prior to arrival")

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1a202c; max-width: 650px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #1A3323; padding: 20px; text-align: center; border-bottom: 3px solid #DBB155;">
                <h1 style="color: #ffffff; margin: 0; font-size: 22px; letter-spacing: 1px;">ROSAPENNA HOTEL & GOLF RESORT</h1>
                <p style="color: #DBB155; margin: 5px 0 0 0; font-size: 13px;">Downings, Sheephaven Bay, Co. Donegal, Ireland</p>
            </div>
            <div style="padding: 25px 20px; border: 1px solid #e2e8f0; border-top: none;">
                <p>Dear {safe_guest_name},</p>
                <p>We are writing to confirm the <b>revised arrangements</b> for your upcoming Stay & Play reservation with Rosapenna Hotel & Golf Resort.</p>
                <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; padding: 15px; border-radius: 4px; margin: 20px 0;">
                    <p style="margin: 0 0 5px 0;"><b>Booking Reference:</b> <span style="color: #1A3323; font-size: 16px;">{safe_ref}</span></p>
                    <p style="margin: 0 0 5px 0;"><b>Status:</b> Confirmed (Amended)</p>
                    <p style="margin: 0 0 5px 0;"><b>Revision Number:</b> Revision #{rev}</p>
                    <p style="margin: 0 0 5px 0;"><b>Summary of Change:</b> {safe_summary}</p>
                    <p style="margin: 0; color: #b91c1c;"><b>Outstanding Balance:</b> EUR {balance_due_str} (Due: {due_date_str})</p>
                </div>
                <p>Your complete revised itinerary, including updated hotel room reservation, links tee times, and verified payment ledger, is attached as an official PDF document to this email.</p>
                {mmb_section}
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 25px 0;" />
                <p style="font-size: 13px; color: #64748b;">
                    If you have any questions or require further assistance, please contact Reservations at <a href="mailto:reservations@rosapenna.ie">reservations@rosapenna.ie</a>.<br/>
                    We look forward to welcoming you to the magnificent links of Donegal.
                </p>
            </div>
        </body>
        </html>
        """

        plain_body = f"""ROSAPENNA HOTEL & GOLF RESORT
Revised Booking Confirmation (Revision #{rev})
Booking Reference: {ref}
Status: Confirmed (Amended)
Dear {guest_name},

Your revised itinerary has been confirmed and is attached as a PDF to this email.
Outstanding Balance: EUR {balance_due_str} (Due: {due_date_str})

For any questions, contact reservations@rosapenna.ie or call +353 (0)74 915 5301.
Warm regards,
Reservations Team
Rosapenna Hotel & Golf Resort
"""

        delivery_id = f"DEL-REV-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()

        if not self.settings.allow_live_send:
            logger.info("Email live send is disabled (EMAIL_ALLOW_LIVE_SEND=false); using simulated sandbox delivery for revised confirmation.")
            self._dispatched_emails.append({
                "delivery_id": delivery_id,
                "template": "revised_booking_confirmation",
                "recipient": recipient,
                "hotel_copy": self.settings.hotel_reservations_email,
                "subject": subject,
                "revision": rev,
                "pdf_size": len(pdf_bytes),
                "dispatched_at": now_str,
            })
            res = EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="revised_booking_confirmation",
                recipient=recipient,
                hotel_notified=True,
                pdf_attached=True,
                subject=subject,
                status="simulated_sandbox",
                sent_at=now_str,
            )
            if idempotency_key:
                self._idempotency_records[idempotency_key] = res
            return res

        try:
            self._send_smtp_email(
                recipient=recipient,
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
                pdf_attachment=pdf_bytes,
                pdf_filename=f"Rosapenna-Revised-Itinerary-{ref}-Rev{rev}.pdf",
                cc_recipient=self.settings.hotel_reservations_email,
            )
            res = EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="revised_booking_confirmation",
                recipient=recipient,
                hotel_notified=True,
                pdf_attached=True,
                subject=subject,
                status="sent",
                sent_at=now_str,
            )
            if idempotency_key:
                self._idempotency_records[idempotency_key] = res
            return res
        except Exception as exc:
            logger.error("Failed to send revised booking confirmation via SMTP: %s", exc)
            return EmailDeliveryResult(
                success=False,
                delivery_id=delivery_id,
                template="revised_booking_confirmation",
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
        safe_guest_name = html.escape(guest_name)
        safe_ref = html.escape(str(ref))
        safe_request_text = html.escape(request_text)
        safe_category = html.escape(category.replace("_", " ").title())
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
                <p>Dear {safe_guest_name},</p>
                <p>We have received your request ({safe_category}) regarding booking <b>{safe_ref}</b>:</p>
                <blockquote style="background-color: #f8fafc; border-left: 4px solid #DBB155; padding: 12px; margin: 15px 0; color: #334155; white-space: pre-wrap;">
                    <i>"{safe_request_text}"</i>
                </blockquote>
                <p>Your request has been forwarded to our Reservations and Concierge team and is currently <b>pending staff review</b>. Please note that special requests and cancellation requests are subject to hotel staff review and verification; confirmed stay dates, golf tee times, inventory, and payment ledger entries remain completely unchanged until authorized and confirmed.</p>
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
                "html_preview": html_body,
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

    def send_staff_outcome_notification(
        self,
        booking: Dict[str, Any],
        request_id: str,
        request_type: str,
        outcome_status: str,
        reason: str,
        guest_message: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> EmailDeliveryResult:
        """Send official notification of staff review outcome for special/cancellation request (Ticket M14)."""
        if idempotency_key and idempotency_key in self._idempotency_records:
            return self._idempotency_records[idempotency_key].model_copy()

        lead = booking.get("lead_guest") or {}
        recipient = lead.get("email") or "guest@example.com"
        ref = booking.get("booking_reference") or booking.get("booking_intent_id", "REF-PENDING")
        guest_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Valued Guest"

        safe_guest_name = html.escape(guest_name)
        safe_ref = html.escape(str(ref))
        safe_req_id = html.escape(str(request_id))
        safe_reason = html.escape(reason)
        safe_guest_message = html.escape(guest_message) if guest_message else None

        if outcome_status == "needs_information":
            status_title = "Information Needed"
            subject = f"Information Needed for Request {request_id} — Booking {ref}"
            explanation = "Our Reservations team requires additional information to proceed with your request."
        elif outcome_status == "rejected":
            status_title = "Request Unable to be Fulfilled"
            subject = f"Update on Request {request_id} — Booking {ref}"
            explanation = "Following review by our Reservations team, we are unable to fulfill this request."
        elif outcome_status == "approved_awaiting_implementation":
            status_title = "Request Approved (Awaiting Implementation)"
            subject = f"Request Approved — Queued for Implementation — Booking {ref}"
            explanation = "Your request has been approved by our Reservations team and is queued for reservation implementation."
        elif outcome_status == "completed":
            status_title = "Request Completed"
            subject = f"Request Completed — Booking {ref}"
            explanation = "Your request has been successfully completed and reflected in your booking arrangements."
        else:
            status_title = "Request Update"
            subject = f"Update on Request {request_id} — Booking {ref}"
            explanation = f"Your request status has been updated to {outcome_status}."

        delivery_id = f"DEL-STAFF-OUTCOME-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()

        guest_msg_html = f'<p><strong>Message from Staff:</strong> {safe_guest_message}</p>' if safe_guest_message else ""

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1a202c; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #1A3323; padding: 15px; text-align: center; border-bottom: 3px solid #DBB155;">
                <h2 style="color: #ffffff; margin: 0; font-size: 18px;">ROSAPENNA HOTEL & GOLF RESORT</h2>
            </div>
            <div style="padding: 20px; border: 1px solid #e2e8f0; border-top: none;">
                <p>Dear {safe_guest_name},</p>
                <p>We are writing to provide an update on your request (Reference: <b>{safe_req_id}</b>) for Booking <b>{safe_ref}</b>.</p>
                <div style="background-color: #f8fafc; border-left: 4px solid #DBB155; padding: 12px; margin: 15px 0; color: #334155;">
                    <p style="margin: 0 0 5px 0;"><strong>Status:</strong> {status_title}</p>
                    <p style="margin: 0 0 5px 0;">{explanation}</p>
                    <p style="margin: 0;"><strong>Details / Reason:</strong> {safe_reason}</p>
                </div>
                {guest_msg_html}
                <p>You can review your updated itinerary and request status at any time in the Rosapenna Manage My Booking portal.</p>
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;" />
                <p style="font-size: 12px; color: #64748b;">Rosapenna Reservations Team · reservations@rosapenna.ie</p>
            </div>
        </body>
        </html>
        """

        plain_body = f"""Dear {guest_name},

We have an update on your request {request_id} for Booking {ref}.
Status: {status_title}
{explanation}
Reason / Details: {reason}
{f"Staff Message: {guest_message}" if guest_message else ""}

Rosapenna Reservations Team · reservations@rosapenna.ie
"""

        if not self.settings.allow_live_send:
            logger.info("Simulating staff request outcome notification dispatch.")
            res = EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="staff_request_outcome",
                recipient=recipient,
                hotel_notified=True,
                pdf_attached=False,
                subject=subject,
                status="simulated_sandbox",
                sent_at=now_str,
            )
            self._dispatched_emails.append({
                "delivery_id": delivery_id,
                "template": "staff_request_outcome",
                "recipient": recipient,
                "hotel_copy": self.settings.hotel_reservations_email,
                "subject": subject,
                "request_id": request_id,
                "outcome_status": outcome_status,
                "html_preview": html_body,
                "dispatched_at": now_str,
            })
            if idempotency_key:
                self._idempotency_records[idempotency_key] = res
            return res

        try:
            self._send_smtp_email(
                recipient=recipient,
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
                cc_recipient=self.settings.hotel_reservations_email,
            )
            res = EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="staff_request_outcome",
                recipient=recipient,
                hotel_notified=True,
                pdf_attached=False,
                subject=subject,
                status="sent",
                sent_at=now_str,
            )
            if idempotency_key:
                self._idempotency_records[idempotency_key] = res
            return res
        except Exception as exc:
            return EmailDeliveryResult(
                success=False,
                delivery_id=delivery_id,
                template="staff_request_outcome",
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
        from rosapenna_backend.config import settings

        test_recipient = self.settings.mmb_test_otp_recipient.strip().casefold()
        allow_test_send = (
            settings.environment == "test"
            and settings.payment_mode == "prommt_test"
            and settings.host == "127.0.0.1"
            and bool(test_recipient)
            and recipient_email.strip().casefold() == test_recipient
        )
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
                <p>Hello {html.escape(recipient_name)},</p>
                <p>Use the verification code below to access your booking <b>{html.escape(booking_reference)}</b>:</p>
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
        logger.info("Processing one-time-code email")

        if not (self.settings.allow_live_send or allow_test_send):
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
            f"<a href='https://rosapenna.ie/manage-booking?ref={quote(str(ref), safe='')}'>Manage My Booking</a>.</p>"
            if self.mmb_gate.is_enabled()
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

    def send_pre_arrival_reminder(
        self,
        booking: Dict[str, Any],
        reminder_type: str,
        recipient_override: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> EmailDeliveryResult:
        """Send specific scheduled pre-arrival communication reminder (Ticket M15).

        Supports:
          - balance_due_first (6 weeks prior)
          - rooming_and_caddy (6 weeks prior / immediate)
          - concierge_check (14 days prior)
          - balance_due_final (5 days prior)
        """
        if idempotency_key and idempotency_key in self._idempotency_records:
            logger.info("Returning replayed pre-arrival reminder for idempotency_key %s", idempotency_key)
            replayed = self._idempotency_records[idempotency_key].model_copy()
            replayed.idempotency_replayed = True
            return replayed

        lead = booking.get("lead_guest") or {}
        recipient = recipient_override or lead.get("email")
        if not recipient:
            return EmailDeliveryResult(
                success=False,
                delivery_id=str(uuid.uuid4()),
                template="pre_arrival_reminder",
                recipient="unknown",
                subject="Pre-Arrival Reminder",
                status="failed",
                sent_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                error_message="Recipient email address is missing.",
            )

        ref = booking.get("booking_reference") or booking.get("booking_intent_id", "REF-PENDING")
        guest_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Valued Guest"
        safe_guest_name = html.escape(guest_name)
        safe_ref = html.escape(str(ref))

        fin = booking.get("financials") or {}
        balance_due_str = str(fin.get("balance_due", "0.00"))
        due_date_str = str(fin.get("balance_due_date") or "Strictly 6 weeks prior to arrival")

        mmb_info = (
            f"<p>To view your itinerary or manage requirements, please visit "
            f"<a href='https://rosapenna.ie/manage-booking?ref={quote(str(ref), safe='')}'>Manage My Booking</a>.</p>"
            if self.mmb_gate.is_enabled()
            else "<p>To manage your booking, please contact our Reservations team at "
                 "<a href='mailto:reservations@rosapenna.ie'>reservations@rosapenna.ie</a> or call +353 (0)74 915 5301.</p>"
        )

        if reminder_type == "rooming_and_caddy":
            subject = f"Pre-Arrival Rooming List & Caddy Preferences – Rosapenna ({ref})"
            title = "Rooming List & Caddy Requirements"
            content = f"""
                <p>Dear {safe_guest_name},</p>
                <p>As your stay at Rosapenna Hotel & Golf Resort approaches (Booking <b>{safe_ref}</b>), we kindly invite you to confirm your rooming party details and caddy requests.</p>
                <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; padding: 15px; border-radius: 4px; margin: 20px 0;">
                    <p style="margin: 0 0 5px 0;"><b>Rooming Party:</b> Please provide names of all occupants sharing each room to expedite your check-in.</p>
                    <p style="margin: 0;"><b>Caddy Requests:</b> Caddies are allocated on request and paid directly to the caddie master (EUR 90 per bag).</p>
                </div>
                {mmb_info}
            """
        elif reminder_type == "concierge_check":
            subject = f"Concierge Welcome & Itinerary Review (14 Days Prior) – Rosapenna ({ref})"
            title = "Concierge Welcome & 14-Day Itinerary Review"
            content = f"""
                <p>Dear {safe_guest_name},</p>
                <p>We look forward to welcoming you to Rosapenna Hotel & Golf Resort in just two weeks (Booking <b>{safe_ref}</b>).</p>
                <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; padding: 15px; border-radius: 4px; margin: 20px 0;">
                    <p style="margin: 0 0 5px 0;"><b>Check-In Time:</b> From 15:00. Check-out is by 11:00.</p>
                    <p style="margin: 0 0 5px 0;"><b>Golf Check-In:</b> Report to the golf pavilion 20 minutes prior to your allocated links tee time.</p>
                    <p style="margin: 0;"><b>Dress Code:</b> Traditional golf attire; soft spikes required on all three courses.</p>
                </div>
                {mmb_info}
            """
        elif reminder_type == "balance_due_final":
            subject = f"Final Notice: Outstanding Balance Due – Rosapenna ({ref})"
            title = "Final Balance Due Reminder (5 Days Prior)"
            content = f"""
                <p>Dear {safe_guest_name},</p>
                <p>This is a final courtesy reminder regarding your outstanding balance for Booking <b>{safe_ref}</b>.</p>
                <div style="background-color: #fff1f2; border: 1px solid #fecdd3; padding: 15px; border-radius: 4px; margin: 20px 0;">
                    <p style="margin: 0 0 5px 0; color: #b91c1c; font-weight: bold;">Outstanding Balance: EUR {balance_due_str}</p>
                    <p style="margin: 0;">Please settle this balance immediately to guarantee your confirmed room and championship links tee times.</p>
                </div>
                {mmb_info}
            """
        else:  # default to balance_due_first
            subject = f"Upcoming Stay & Balance Reminder (6 Weeks Prior) – Rosapenna ({ref})"
            title = "Balance Payment Reminder (6 Weeks Prior)"
            content = f"""
                <p>Dear {safe_guest_name},</p>
                <p>We are looking forward to welcoming you to Rosapenna Hotel & Golf Resort for Booking <b>{safe_ref}</b>.</p>
                <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; padding: 15px; border-radius: 4px; margin: 20px 0;">
                    <p style="margin: 0 0 5px 0; color: #b91c1c; font-weight: bold;">Remaining Balance Due: EUR {balance_due_str}</p>
                    <p style="margin: 0;"><b>Due Date:</b> {due_date_str}</p>
                </div>
                {mmb_info}
            """

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1a202c; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #1A3323; padding: 15px; text-align: center; border-bottom: 3px solid #DBB155;">
                <h2 style="color: #ffffff; margin: 0; font-size: 18px;">ROSAPENNA HOTEL & GOLF RESORT</h2>
                <p style="color: #DBB155; margin: 5px 0 0 0; font-size: 12px;">{title}</p>
            </div>
            <div style="padding: 20px; border: 1px solid #e2e8f0; border-top: none;">
                {content}
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;" />
                <p style="font-size: 12px; color: #64748b;">
                    Reservations Team &bull; Rosapenna Hotel & Golf Resort &bull; reservations@rosapenna.ie
                </p>
            </div>
        </body>
        </html>
        """

        plain_body = f"""ROSAPENNA HOTEL & GOLF RESORT
{title}
Booking Reference: {ref}
Dear {guest_name},
Please contact reservations@rosapenna.ie or call +353 (0)74 915 5301.
"""

        delivery_id = f"DEL-REM-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()

        if not self.settings.allow_live_send:
            self._dispatched_emails.append({
                "delivery_id": delivery_id,
                "template": "pre_arrival_reminder",
                "reminder_type": reminder_type,
                "recipient": recipient,
                "subject": subject,
                "dispatched_at": now_str,
            })
            res = EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="pre_arrival_reminder",
                recipient=recipient,
                hotel_notified=False,
                pdf_attached=False,
                subject=subject,
                status="simulated_sandbox",
                sent_at=now_str,
            )
            if idempotency_key:
                self._idempotency_records[idempotency_key] = res
            return res

        try:
            self._send_smtp_email(
                recipient=recipient,
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
            )
            res = EmailDeliveryResult(
                success=True,
                delivery_id=delivery_id,
                template="pre_arrival_reminder",
                recipient=recipient,
                hotel_notified=False,
                pdf_attached=False,
                subject=subject,
                status="sent",
                sent_at=now_str,
            )
            if idempotency_key:
                self._idempotency_records[idempotency_key] = res
            return res
        except Exception as exc:
            return EmailDeliveryResult(
                success=False,
                delivery_id=delivery_id,
                template="pre_arrival_reminder",
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

        if not self.settings.smtp_host or not self.settings.from_email:
            raise ValueError("SMTP host and sender address must be configured")
        if not self.settings.smtp_user or not password:
            raise ValueError("SMTP username and password must both be configured")

        context = ssl.create_default_context()
        if self.settings.smtp_port == 465 or self.settings.smtp_secure:
            connection = smtplib.SMTP_SSL(
                self.settings.smtp_host, self.settings.smtp_port, timeout=15, context=context
            )
        else:
            connection = smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=15)

        with connection as server:
            if not (self.settings.smtp_port == 465 or self.settings.smtp_secure):
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            if self.settings.smtp_user:
                server.login(self.settings.smtp_user, password)
            refused = server.send_message(msg, from_addr=self.settings.from_email, to_addrs=recipients)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)


# ==============================================================================
# Ticket N07: Dedicated Render & Transport Adapters
# ==============================================================================


@dataclass
class TransportSubmitOutcome:
    """Outcome of submitting a confirmation message to a transport."""

    state: str  # ConfirmationDeliveryState
    evidence_source: str
    accepted_recipient: Optional[str] = None
    provider_reference: Optional[str] = None
    error_code: Optional[str] = None
    error_details: Optional[str] = None


class ConfirmationTransportProtocol:
    """Interface for N07 confirmation transport."""

    def submit(
        self,
        message_bytes: bytes,
        envelope_recipient: str,
        message_id: str,
        attempt_id: str,
    ) -> TransportSubmitOutcome:
        raise NotImplementedError


class DisabledConfirmationTransport(ConfirmationTransportProtocol):
    """Fails closed and denies any real or simulated transmission."""

    def submit(
        self,
        message_bytes: bytes,
        envelope_recipient: str,
        message_id: str,
        attempt_id: str,
    ) -> TransportSubmitOutcome:
        return TransportSubmitOutcome(
            state="blocked_dependency",
            evidence_source="disabled_transport",
            error_code="TRANSPORT_DISABLED",
            error_details="Live email submission is disabled for N07 structure.",
        )


class RecordingConfirmationTransport(ConfirmationTransportProtocol):
    """Test transport that records messages locally to disk.

    Guarantees:
    - Enforces that envelope_recipient ends with '.invalid' test domain.
    - Rejects real recipient domains to prevent accidental email leakage.
    - Simulates controllable outcomes (success, refusal, permanent_failure, timeout).
    """

    def __init__(
        self,
        recording_dir: Path | str,
        simulated_behavior: Optional[Dict[str, str]] = None,
        owned_root: Optional[Path | str] = None,
    ):
        configured_dir = Path(recording_dir).expanduser()
        if not configured_dir.is_absolute():
            raise ValueError("Recording directory must be an absolute path")
        if configured_dir.is_symlink():
            raise ValueError("Recording directory cannot be a symbolic link")
        configured_dir.mkdir(parents=True, exist_ok=True)
        self.recording_dir = configured_dir.resolve(strict=True)
        if not self.recording_dir.is_dir():
            raise ValueError("Recording path must be a directory")
        root = Path(owned_root).expanduser().resolve(strict=True) if owned_root else self.recording_dir
        if self.recording_dir != root and root not in self.recording_dir.parents:
            raise ValueError("Recording directory must stay inside its owned test root")
        self._configured_dir = configured_dir
        self._owned_root = root
        self.simulated_behavior = simulated_behavior or {}
        self.recorded_submissions: List[Dict[str, Any]] = []

    @staticmethod
    def _valid_test_mailbox(address: str) -> bool:
        if not isinstance(address, str) or len(address) > 254 or any(ord(char) < 33 or ord(char) == 127 for char in address):
            return False
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,61}[A-Za-z0-9])?\.invalid", address):
            return False
        local, domain = address.rsplit("@", 1)
        return ".." not in local and ".." not in domain and not local.endswith(".")

    def submit(
        self,
        message_bytes: bytes,
        envelope_recipient: str,
        message_id: str,
        attempt_id: str,
    ) -> TransportSubmitOutcome:
        clean_recipient = envelope_recipient.strip().lower() if isinstance(envelope_recipient, str) else ""
        if not self._valid_test_mailbox(clean_recipient):
            return TransportSubmitOutcome(
                state="permanent_failure",
                evidence_source="recording_adapter",
                error_code="NON_TEST_RECIPIENT_REJECTED",
                error_details="Recipient must be one valid .invalid test mailbox",
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", attempt_id):
            return TransportSubmitOutcome(
                state="permanent_failure",
                evidence_source="recording_adapter",
                error_code="INVALID_ATTEMPT_ID",
            )
        if len(message_bytes) > 4 * 1024 * 1024:
            return TransportSubmitOutcome(
                state="permanent_failure",
                evidence_source="recording_adapter",
                error_code="MESSAGE_TOO_LARGE",
            )

        behavior = self.simulated_behavior.get(clean_recipient, self.simulated_behavior.get("default", "success"))

        if behavior == "refusal":
            return TransportSubmitOutcome(
                state="retryable_failure",
                evidence_source="recording_adapter",
                error_code="SIMULATED_RECIPIENT_REFUSAL",
                error_details="Simulated temporary SMTP mailbox unavailable (450)",
            )
        elif behavior == "permanent_failure":
            return TransportSubmitOutcome(
                state="permanent_failure",
                evidence_source="recording_adapter",
                error_code="SIMULATED_PERMANENT_REFUSAL",
                error_details="Simulated permanent 550 recipient rejected",
            )
        elif behavior == "timeout":
            return TransportSubmitOutcome(
                state="unknown",
                evidence_source="recording_adapter",
                error_code="SIMULATED_TIMEOUT_POST_SUBMISSION",
                error_details="Simulated socket timeout while waiting for 250 response",
            )

        # Use a generated filename. Never place an address or request value in a path.
        if self._configured_dir.resolve(strict=True) != self.recording_dir:
            raise ValueError("Recording directory changed after transport setup")
        if self.recording_dir != self._owned_root and self._owned_root not in self.recording_dir.parents:
            raise ValueError("Recording directory is outside its owned test root")
        file_path = self.recording_dir / f"confirmation-{uuid.uuid4().hex}.eml"
        if file_path.resolve(strict=False).parent != self.recording_dir:
            raise ValueError("Recording output path is outside the test sink")
        with file_path.open("xb") as output:
            output.write(message_bytes)

        record = {
            "attempt_id": attempt_id,
            "recipient": clean_recipient,
            "message_id": message_id,
            "bytes_length": len(message_bytes),
            "file_path": str(file_path),
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self.recorded_submissions.append(record)

        return TransportSubmitOutcome(
            state="recorded_test",
            evidence_source="recording_adapter",
            accepted_recipient=clean_recipient,
            provider_reference=f"REC-{attempt_id[:8]}",
        )


def render_n07_email_message(
    snapshot: BookingConfirmationSnapshot,
    recipient_role: Literal["guest", "hotel"],
    pdf_bytes: bytes,
    attempt_id: str,
    recipient_address: Optional[str] = None,
    message_date: Optional[str] = None,
    message_id: Optional[str] = None,
    from_email: str = "noreply@rosapenna.invalid",
    from_name: str = "Rosapenna Hotel & Golf Resort",
) -> tuple[str, str, str, bytes, str]:
    """Compile email subject, plain text, HTML, and MIME bytes for Ticket N07.

    Returns:
        (subject, plain_text, html_body, mime_bytes, message_id)
    """
    ref = snapshot.booking_reference
    rev = snapshot.booking_revision
    lead = snapshot.lead_guest
    guest_name = f"{lead.first_name} {lead.last_name}"

    subject = f"TEST — NOT A REAL BOOKING CONFIRMATION — Rosapenna Hotel & Golf Resort ({ref})"
    message_id = message_id or f"<{ref}.r{rev}.{recipient_role}.{attempt_id}@rosapenna.invalid>"

    target_recipient = recipient_address or (lead.email if recipient_role == "guest" else "hotel@rosapenna.invalid")
    if not RecordingConfirmationTransport._valid_test_mailbox(target_recipient):
        raise ValueError("N07 message recipient must be one .invalid test mailbox")
    if not re.fullmatch(r"<[^<>\s\r\n]{1,240}>", message_id):
        raise ValueError("N07 Message-ID is malformed")

    # Plain text
    plain_body = f"""*** TEST — NOT A REAL BOOKING CONFIRMATION ***
This is a synthetic test artifact for Rosapenna booking confirmation verification.

Booking Reference: {ref} (Revision #{rev})
Status: Confirmed (Test)
Lead Guest: {guest_name}
Stay Dates: {snapshot.stay.arrival_date} to {snapshot.stay.departure_date}

Rooms Booked:
"""
    for r in snapshot.stay.rooms:
        plain_body += f"- {r.room_group_id}: {r.room_category} (Qty: {r.room_quantity})\n"

    if snapshot.golf_rounds:
        plain_body += "\nChampionship Golf Itinerary:\n"
        for g in snapshot.golf_rounds:
            names = f"; Golfers: {', '.join(g.golfer_names)}" if g.golfer_names else ""
            plain_body += f"- {g.date} at {g.tee_time}: {g.course_name} ({g.players} players, {g.holes} holes){names}\n"

    fin = snapshot.financials
    plain_body += f"""
Financial Settlement:
- Total Stay & Play: {fin.currency} {fin.total_amount}
- Deposit Paid: {fin.currency} {fin.deposit_paid}
- Total Verified Paid: {fin.currency} {fin.verified_paid_total}
- Remaining Balance Due: {fin.currency} {fin.balance_due} (Due: {fin.balance_due_date})

Official PDF itinerary is attached.
"""
    if snapshot.payments:
        plain_body += "Verified Payments:\n"
        for payment in snapshot.payments:
            plain_body += f"- {payment.payment_purpose.title()}: {payment.currency} {payment.amount} (verified {payment.verified_at})\n"

    # HTML body
    rooms_html = "".join(
        f"<li><b>{html.escape(r.room_group_id)}</b>: {html.escape(r.room_category)} (Qty: {r.room_quantity})</li>"
        for r in snapshot.stay.rooms
    )
    golf_html = "".join(
        f"<li><b>{html.escape(g.date)}</b> at {html.escape(g.tee_time)}: {html.escape(g.course_name)} ({g.players} players, {g.holes} holes)"
        + (f"; Golfers: {html.escape(', '.join(g.golfer_names))}" if g.golfer_names else "")
        + "</li>"
        for g in snapshot.golf_rounds
    )
    if golf_html:
        golf_section = f"<h3>Championship Golf</h3><ul>{golf_html}</ul>"
    else:
        golf_section = ""

    html_body = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{html.escape(subject)}</title>
</head>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #0F172A; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="background-color: #FEE2E2; border: 2px solid #EF4444; color: #B91C1C; padding: 12px; text-align: center; font-weight: bold; border-radius: 4px; margin-bottom: 20px;">
        TEST — NOT A REAL BOOKING CONFIRMATION
    </div>

    <h2 style="color: #1A3323; margin-top: 0;">Rosapenna Hotel &amp; Golf Resort</h2>
    <p>Dear {html.escape(guest_name)},</p>
    <p>Thank you for booking your Stay &amp; Play package. This is your test confirmation record.</p>

    <div style="background-color: #F8F9FA; border: 1px solid #E2E8F0; padding: 15px; border-radius: 4px; margin: 20px 0;">
        <p style="margin: 0 0 8px 0;"><b>Booking Reference:</b> {html.escape(ref)} (Revision #{rev})</p>
        <p style="margin: 0 0 8px 0;"><b>Dates:</b> {html.escape(snapshot.stay.arrival_date)} to {html.escape(snapshot.stay.departure_date)}</p>
        <p style="margin: 0;"><b>Status:</b> Confirmed (Test)</p>
    </div>

    <h3>Accommodation</h3>
    <ul>{rooms_html}</ul>

    {golf_section}

    <h3>Financial Summary</h3>
    <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
        <tr>
            <td style="padding: 6px 0; border-bottom: 1px solid #E2E8F0;">Total Amount:</td>
            <td style="padding: 6px 0; border-bottom: 1px solid #E2E8F0; text-align: right; font-weight: bold;">{html.escape(fin.currency)} {html.escape(fin.total_amount)}</td>
        </tr>
        <tr>
            <td style="padding: 6px 0; border-bottom: 1px solid #E2E8F0;">Deposit Paid:</td>
            <td style="padding: 6px 0; border-bottom: 1px solid #E2E8F0; text-align: right; font-weight: bold; color: #166534;">{html.escape(fin.currency)} {html.escape(fin.deposit_paid)}</td>
        </tr>
        <tr>
            <td style="padding: 6px 0; border-bottom: 1px solid #E2E8F0;">Total Verified Paid:</td>
            <td style="padding: 6px 0; border-bottom: 1px solid #E2E8F0; text-align: right; font-weight: bold; color: #166534;">{html.escape(fin.currency)} {html.escape(fin.verified_paid_total)}</td>
        </tr>
        <tr>
            <td style="padding: 6px 0; font-weight: bold;">Remaining Balance:</td>
            <td style="padding: 6px 0; text-align: right; font-weight: bold; color: #B91C1C;">{html.escape(fin.currency)} {html.escape(fin.balance_due)}</td>
        </tr>
        <tr>
            <td style="padding: 4px 0; font-size: 13px; color: #64748B;">Balance Due Date:</td>
            <td style="padding: 4px 0; text-align: right; font-size: 13px; color: #64748B;">{html.escape(fin.balance_due_date)}</td>
        </tr>
    </table>

    {"<h3>Verified Payments</h3><ul>" + "".join(f"<li>{html.escape(payment.payment_purpose.title())}: {html.escape(payment.currency)} {html.escape(payment.amount)} (verified {html.escape(payment.verified_at)})</li>" for payment in snapshot.payments) + "</ul>" if snapshot.payments else ""}

    <p>Your official itinerary document is attached to this email as a PDF.</p>
    <hr style="border: none; border-top: 1px solid #E2E8F0; margin: 20px 0;">
    <p style="font-size: 12px; color: #64748B; text-align: center;">Rosapenna Hotel &amp; Golf Resort &bull; Downings, Co. Donegal, Ireland</p>
</body>
</html>
"""

    # Build MIME message
    msg = MIMEMultipart("mixed")
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = target_recipient
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if message_date:
        parsed_date = dt.datetime.fromisoformat(message_date.replace("Z", "+00:00"))
        if parsed_date.tzinfo is None:
            raise ValueError("Confirmation message date must include a time zone")
    else:
        parsed_date = dt.datetime.now(dt.timezone.utc)
    msg["Date"] = email.utils.format_datetime(parsed_date)
    msg["X-Rosapenna-Test"] = "true"
    msg["X-Booking-Reference"] = ref
    msg["X-Recipient-Role"] = recipient_role

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(plain_body, "plain", "utf-8"))
    alt_part.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt_part)

    if pdf_bytes:
        pdf_part = MIMEApplication(pdf_bytes, _subtype="pdf")
        pdf_filename = f"Rosapenna-Itinerary-{ref}.pdf"
        pdf_part.add_header("Content-Disposition", "attachment", filename=pdf_filename)
        msg.attach(pdf_part)

    mime_bytes = msg.as_bytes()
    if len(mime_bytes) > 4 * 1024 * 1024:
        raise ValueError("Total MIME message exceeds 4 MiB size limit")

    return subject, plain_body, html_body, mime_bytes, message_id


def render_n08_pre_arrival_message(
    booking: EligibleBookingInput,
    reminder_type: Literal["balance_due_first", "rooming_and_caddy"],
    policy: PreArrivalPolicyDoc,
    attempt_id: str,
    action_url: Optional[str] = None,
    mmb_enabled: bool = False,
    message_id: Optional[str] = None,
    message_date: Optional[str] = None,
    is_test: bool = True,
) -> tuple[str, str, str, bytes, str, str]:
    """Compile email subject, plain text, HTML, MIME bytes, and content hash for Ticket N08.

    Returns:
        (subject, plain_text, html_body, mime_bytes, message_id, message_hash)
    """
    ref = booking.booking_reference
    rev = booking.revision
    guest_name = booking.lead_name or "Valued Guest"
    safe_guest_name = html.escape(guest_name)
    safe_ref = html.escape(str(ref))
    arr_date_str = booking.arrival_date.isoformat()

    # Logical headers verification
    for val, name in [
        (policy.sender_name, "sender_name"),
        (policy.sender_email, "sender_email"),
        (booking.lead_email, "lead_email"),
    ]:
        if "\r" in val or "\n" in val:
            raise ValueError(f"CR/LF characters are strictly forbidden in header {name}")

    target_recipient = booking.lead_email.strip().lower()
    if is_test and not RecordingConfirmationTransport._valid_test_mailbox(target_recipient):
        raise ValueError("N08 test message recipient must be one valid .invalid test mailbox")

    message_id = message_id or f"<{ref}.r{rev}.{reminder_type}.{attempt_id}@rosapenna.invalid>"
    if not re.fullmatch(r"<[^<>\s\r\n]{1,240}>", message_id):
        raise ValueError("N08 Message-ID is malformed or contains invalid characters")

    test_prefix = "TEST — NOT A REAL EMAIL — " if is_test else ""

    # Validate action url if supplied
    safe_action_link_html = ""
    safe_action_link_plain = ""
    if action_url:
        clean_url = action_url.strip()
        if "\r" in clean_url or "\n" in clean_url:
            raise ValueError("CR/LF forbidden in action URL")
        if len(clean_url) > 2000:
            raise ValueError("Action URL exceeds maximum allowed length")
        parsed = urlsplit(clean_url)
        if parsed.scheme != "https":
            raise ValueError("Action URL must use secure HTTPS")
        if parsed.username or parsed.password:
            raise ValueError("Action URL must not contain user credentials")
        if parsed.port and parsed.port not in (443, 8443):
            raise ValueError("Action URL uses unexpected port")
        allowed_hosts = {"rosapenna.ie", "www.rosapenna.ie", "localhost", "127.0.0.1"}
        host = (parsed.hostname or "").lower()
        if host not in allowed_hosts and not (is_test and host.endswith(".invalid")):
            raise ValueError(f"Action URL host '{parsed.hostname}' is not an approved destination")
        safe_href = html.escape(clean_url, quote=True)
        if reminder_type == "balance_due_first":
            safe_action_link_html = (
                f'<p style="margin: 20px 0;"><a href="{safe_href}" style="background-color: #1A3323; '
                f'color: #ffffff; padding: 12px 20px; text-decoration: none; border-radius: 4px; '
                f'font-weight: bold; display: inline-block;">Settle Outstanding Balance Online</a></p>'
            )
            safe_action_link_plain = f"\nSettle balance online: {clean_url}\n"
        else:
            safe_action_link_html = (
                f'<p style="margin: 20px 0;"><a href="{safe_href}" style="background-color: #1A3323; '
                f'color: #ffffff; padding: 12px 20px; text-decoration: none; border-radius: 4px; '
                f'font-weight: bold; display: inline-block;">Complete Preferences Form Online</a></p>'
            )
            safe_action_link_plain = f"\nComplete preferences online: {clean_url}\n"
    elif mmb_enabled:
        mmb_url = f"https://rosapenna.ie/manage-booking?ref={quote(ref, safe='')}"
        safe_action_link_html = (
            f'<p>To view your itinerary or manage details, please visit '
            f'<a href="{mmb_url}">Manage My Booking</a>.</p>'
        )
        safe_action_link_plain = f"\nManage booking: {mmb_url}\n"
    else:
        safe_action_link_html = (
            '<p>To discuss your booking or requirements, please contact our Reservations team at '
            '<a href="mailto:reservations@rosapenna.ie">reservations@rosapenna.ie</a> '
            'or call +353 (0)74 915 5301.</p>'
        )
        safe_action_link_plain = (
            "\nContact Reservations at reservations@rosapenna.ie or call +353 (0)74 915 5301.\n"
        )

    if reminder_type == "balance_due_first":
        bal_lead = policy.balance_request_lead_days or 42
        subject = f"{test_prefix}Pre-Arrival Balance Payment Request – Rosapenna ({ref})"
        title = f"Balance Payment Request ({bal_lead} Days Prior)"
        bal_due_date = (booking.arrival_date - dt.timedelta(days=policy.balance_due_lead_days)).isoformat()
        bal_str = f"{booking.currency} {booking.balance_due:.2f}"

        plain_body = f"""{test_prefix}ROSAPENNA HOTEL & GOLF RESORT
{title}
Booking Reference: {ref} (Revision #{rev})
Arrival Date: {arr_date_str}

Dear {guest_name},

We look forward to welcoming you to Rosapenna Hotel & Golf Resort.
Your remaining balance of {bal_str} is due strictly by {bal_due_date}.
{safe_action_link_plain}
Reservations Team
Rosapenna Hotel & Golf Resort
reservations@rosapenna.ie
"""
        content_html = f"""
            <p>Dear {safe_guest_name},</p>
            <p>We are delighted to welcome you to Rosapenna Hotel & Golf Resort for your upcoming stay.</p>
            <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; padding: 15px; border-radius: 4px; margin: 20px 0;">
                <p style="margin: 0 0 8px 0; font-size: 15px;"><b>Booking Reference:</b> {safe_ref} (Revision #{rev})</p>
                <p style="margin: 0 0 8px 0; font-size: 15px;"><b>Arrival Date:</b> {html.escape(arr_date_str)}</p>
                <p style="margin: 0 0 8px 0; color: #b91c1c; font-weight: bold; font-size: 16px;">
                    Remaining Balance Due: {html.escape(bal_str)}
                </p>
                <p style="margin: 0; font-size: 14px; color: #475569;">
                    <b>Due Date:</b> {html.escape(bal_due_date)}
                </p>
            </div>
            {safe_action_link_html}
        """
    else:  # rooming_and_caddy
        subject = f"{test_prefix}Pre-Arrival Rooming List & Caddy Preferences – Rosapenna ({ref})"
        title = "Rooming List & Caddy Requirements (6 Weeks Prior)"

        plain_body = f"""{test_prefix}ROSAPENNA HOTEL & GOLF RESORT
{title}
Booking Reference: {ref} (Revision #{rev})
Arrival Date: {arr_date_str}

Dear {guest_name},

As your stay at Rosapenna Hotel & Golf Resort approaches, please provide your rooming party details (names of occupants) and any caddy requirements.

Please note: Caddies are allocated subject to availability and paid directly at the resort.
{safe_action_link_plain}
Reservations Team
Rosapenna Hotel & Golf Resort
reservations@rosapenna.ie
"""
        content_html = f"""
            <p>Dear {safe_guest_name},</p>
            <p>As your stay at Rosapenna Hotel & Golf Resort approaches for Booking <b>{safe_ref}</b>, we kindly invite you to confirm your rooming party details and caddy preferences.</p>
            <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; padding: 15px; border-radius: 4px; margin: 20px 0;">
                <p style="margin: 0 0 8px 0;"><b>Rooming Party:</b> Please provide the names of all occupants sharing each room to ensure a seamless check-in experience.</p>
                <p style="margin: 0;"><b>Caddy Requests:</b> Caddies are allocated on request subject to availability and settled directly at the resort.</p>
            </div>
            {safe_action_link_html}
        """

    test_banner_html = (
        '<div style="background-color: #FEF3C7; border: 1px solid #F59E0B; padding: 10px; '
        'margin-bottom: 15px; border-radius: 4px; text-align: center; font-weight: bold; color: #92400E;">'
        '*** TEST — NOT A REAL EMAIL — SYNTHETIC VERIFICATION ARTIFACT ***'
        '</div>'
        if is_test
        else ""
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1a202c; max-width: 600px; margin: 0 auto; padding: 20px;">
    {test_banner_html}
    <div style="background-color: #1A3323; padding: 15px; text-align: center; border-bottom: 3px solid #DBB155;">
        <h2 style="color: #ffffff; margin: 0; font-size: 18px;">ROSAPENNA HOTEL &amp; GOLF RESORT</h2>
        <p style="color: #DBB155; margin: 5px 0 0 0; font-size: 12px;">{html.escape(title)}</p>
    </div>
    <div style="padding: 20px; border: 1px solid #e2e8f0; border-top: none;">
        {content_html}
        <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;" />
        <p style="font-size: 12px; color: #64748b; text-align: center;">
            Reservations Team &bull; Rosapenna Hotel &amp; Golf Resort &bull; reservations@rosapenna.ie
        </p>
    </div>
</body>
</html>"""

    # Build MIME
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{policy.sender_name} <{policy.sender_email}>"
    msg["To"] = target_recipient
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if policy.reply_to:
        msg["Reply-To"] = policy.reply_to

    if message_date:
        parsed_date = dt.datetime.fromisoformat(message_date.replace("Z", "+00:00"))
        if parsed_date.tzinfo is None:
            raise ValueError("Message date must include a timezone")
    else:
        parsed_date = dt.datetime.now(dt.timezone.utc)
    msg["Date"] = email.utils.format_datetime(parsed_date)
    msg["X-Rosapenna-Test"] = "true" if is_test else "false"
    msg["X-Booking-Reference"] = ref
    msg["X-Reminder-Type"] = reminder_type

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    mime_bytes = msg.as_bytes()
    if len(mime_bytes) > 2 * 1024 * 1024:
        raise ValueError("N08 pre-arrival email MIME exceeds 2 MiB limit")

    content_hash_payload = f"{subject}|{plain_body}|{html_body}".encode("utf-8")
    message_hash = hashlib.sha256(content_hash_payload).hexdigest()

    return subject, plain_body, html_body, mime_bytes, message_id, message_hash
