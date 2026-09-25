import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class ConversationStatus(str, enum.Enum):
    active = "active"
    escalated = "escalated"
    closed = "closed"


class MessageDirection(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class MessageType(str, enum.Enum):
    text = "text"
    interactive = "interactive"
    document = "document"
    image = "image"
    audio = "audio"
    system = "system"


class LeadStatus(str, enum.Enum):
    new = "new"
    contacted = "contacted"
    enrolled = "enrolled"
    discarded = "discarded"


class PaymentStatus(str, enum.Enum):
    pending = "pending"  # link creado, esperando el pago
    approved = "approved"
    declined = "declined"
    voided = "voided"
    error = "error"


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wa_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    profile_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_new: Mapped[bool] = mapped_column(Boolean, default=True)
    needs_human: Mapped[bool] = mapped_column(Boolean, default=False)
    human_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    conversations: Mapped[list["Conversation"]] = relationship(back_populates="contact")
    leads: Mapped[list["Lead"]] = relationship(back_populates="contact")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), index=True)
    status: Mapped[ConversationStatus] = mapped_column(
        Enum(ConversationStatus, name="conversation_status"), default=ConversationStatus.active
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contact: Mapped["Contact"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True, index=True)
    direction: Mapped[MessageDirection] = mapped_column(Enum(MessageDirection, name="message_direction"))
    message_type: Mapped[MessageType] = mapped_column(Enum(MessageType, name="message_type"))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_calls: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = _uuid_pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), index=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    program_interest: Mapped[str | None] = mapped_column(String(255), nullable=True)
    preferred_schedule: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus, name="lead_status"), default=LeadStatus.new)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contact: Mapped["Contact"] = relationship(back_populates="leads")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(100))
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    # Meta almacena el media_id subido ~30 días; lo cacheamos para no resubir en cada envío.
    whatsapp_media_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    whatsapp_media_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentRequest(Base):
    """Solicitud de pago de cuota: datos que dio el estudiante + link de Wompi + resultado final."""

    __tablename__ = "payment_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    reference: Mapped[str] = mapped_column(String(36), unique=True)
    national_id: Mapped[str] = mapped_column(String(20), index=True)
    contract_number: Mapped[str] = mapped_column(String(40), index=True)
    account_number: Mapped[str] = mapped_column(String(40))
    wompi_payment_link_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    payment_url: Mapped[str] = mapped_column(String(255))
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), default=PaymentStatus.pending
    )
    amount_in_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    payment_method_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    wompi_transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    events: Mapped[list["PaymentEvent"]] = relationship(
        back_populates="payment_request", order_by="PaymentEvent.received_at"
    )


class PaymentEvent(Base):
    """Evidencia inmutable: cada evento recibido de Wompi (payload crudo + transacción consultada)."""

    __tablename__ = "payment_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    payment_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_requests.id"), nullable=True, index=True
    )
    event: Mapped[str] = mapped_column(String(64))
    wompi_transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    transaction_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    amount_in_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    environment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    checksum: Mapped[str] = mapped_column(String(64))
    event_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    verified_transaction: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    payment_request: Mapped["PaymentRequest | None"] = relationship(back_populates="events")
