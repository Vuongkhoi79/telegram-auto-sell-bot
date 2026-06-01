from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PaymentConfig:
    bank_name: str = ""
    bank_account: str = ""
    bank_account_name: str = ""
    qr_url: str = ""
    note_prefix: str = "AI_DAILY"


class PaymentService:
    def __init__(self, config: PaymentConfig):
        self.config = config

    def has_bank_config(self) -> bool:
        return bool(self.config.bank_name and self.config.bank_account and self.config.bank_account_name)

    def build_transfer_note(self, order_id: str, machine_id: str) -> str:
        suffix = machine_id.strip().upper().replace("-", "")[:8]
        return f"{self.config.note_prefix}_{order_id}_{suffix}"

    def build_payment_text(self, amount_vnd: int, order_id: str, machine_id: str) -> str:
        amount_text = f"{amount_vnd:,}".replace(",", ".")
        lines = [
            "Machine ID nay da nhan free 90 ngay.",
            f"De dung vinh vien, phi kich hoat la {amount_text}đ.",
            f"Order ID: {order_id}",
        ]
        if self.has_bank_config():
            note = self.build_transfer_note(order_id, machine_id)
            lines.extend(
                [
                    "Thong tin chuyen khoan:",
                    f"- Ngan hang: {self.config.bank_name}",
                    f"- So tai khoan: {self.config.bank_account}",
                    f"- Chu tai khoan: {self.config.bank_account_name}",
                    f"- Noi dung: {note}",
                ]
            )
        else:
            lines.append("Vui long lien he admin de nhan thong tin thanh toan.")
        return "\n".join(lines)

    def build_qr_caption(self, amount_vnd: int, order_id: str, machine_id: str) -> str:
        return self.build_payment_text(amount_vnd, order_id, machine_id)
