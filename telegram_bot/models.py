# models.py
"""
Production-grade data models for the Telegram digital product bot.
- Uses SQLAlchemy ORM
- Self-contained Base definition to prevent circular imports
- Includes comprehensive Enums and Indexes
"""

import enum
from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    Enum as SQLEnum,
    Index,
    Text,
    func
)
from sqlalchemy.orm import relationship, declarative_base

# Create Base here to avoid circular imports with db.py
Base = declarative_base()

# =========================
# ENUMS
# =========================

class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"

class OrderStatus(str, enum.Enum):
    PENDING = "pending"   # Just in case we add async processing later
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class TopUpStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class DeliveryType(str, enum.Enum):
    TEXT = "text"
    FILE = "file"

# =========================
# MODELS
# =========================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(String(32), unique=True, nullable=False, index=True)
    username = Column(String(64), nullable=True)
    balance_usd = Column(Float, default=0.0, nullable=False)
    language = Column(String(5), default="en", nullable=False)  # en, ru, ar
    role = Column(SQLEnum(UserRole), default=UserRole.USER, nullable=False)
    is_banned = Column(Boolean, default=False, nullable=False)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    orders = relationship("Order", back_populates="user", lazy="dynamic")
    topups = relationship("TopUp", back_populates="user", lazy="dynamic")

    def __repr__(self):
        return f"<User id={self.id} tg_id={self.telegram_id} bal={self.balance_usd}>"


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    price_usd = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    # default load of codes is discouraged if there are thousands, so we use lazy='dynamic' or separate queries
    codes = relationship("ProductCode", back_populates="product", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Product id={self.id} name={self.name} price={self.price_usd}>"


class ProductCode(Base):
    __tablename__ = "product_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    code = Column(Text, nullable=False, unique=True)
    is_sold = Column(Boolean, default=False, nullable=False)
    
    # Tracking when it was sold
    sold_at = Column(DateTime(timezone=True), nullable=True)

    product = relationship("Product", back_populates="codes")

    # Composite index for faster stock lookups: Find all unsold codes for a specific product
    __table_args__ = (
        Index("idx_product_is_sold", "product_id", "is_sold"),
    )

    def __repr__(self):
        return f"<ProductCode id={self.id} product_id={self.product_id} sold={self.is_sold}>"


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    product_code_id = Column(Integer, ForeignKey("product_codes.id"), nullable=False, unique=True)

    # Snapshot of price at time of purchase (in case product price changes later)
    price_usd = Column(Float, nullable=False)
    
    delivery_type = Column(SQLEnum(DeliveryType), nullable=False)
    status = Column(SQLEnum(OrderStatus), default=OrderStatus.COMPLETED, nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    user = relationship("User", back_populates="orders")
    product = relationship("Product")
    product_code = relationship("ProductCode")

    def __repr__(self):
        return f"<Order id={self.id} user={self.user_id} product={self.product_id}>"


class TopUp(Base):
    __tablename__ = "topups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount_usd = Column(Float, nullable=False)
    
    # Payment method details
    # Could be "TXID: 12345" or "Note: MyUsername Transfer"
    txid_or_note = Column(String(255), nullable=False)
    
    status = Column(SQLEnum(TopUpStatus), default=TopUpStatus.PENDING, nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approved_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="topups")

    # Index for admins to quickly find pending topups
    __table_args__ = (
        Index("idx_topup_status", "status"),
    )

    def __repr__(self):
        return f"<TopUp id={self.id} user={self.user_id} amt={self.amount_usd} status={self.status}>"


class AdminActionLog(Base):
    __tablename__ = "admin_action_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(255), nullable=False) # e.g. "banned user 123", "added stock 50"
    details = Column(Text, nullable=True) # Optional JSON or extra details
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    admin = relationship("User")

    def __repr__(self):
        return f"<AdminAction admin={self.admin_id} action={self.action}>"

# =========================
# TEST SCRIPT
# =========================
if __name__ == "__main__":
    from sqlalchemy import create_engine
    
    # In-memory DB for quick schema verification
    engine = create_engine("sqlite:///:memory:")
    print("Creating tables...")
    Base.metadata.create_all(engine)
    print("Tables created successfully ✔")