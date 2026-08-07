from dataclasses import dataclass

@dataclass(frozen=True)
class AppointmentPayment:
    date: str
    submitted_amount: float
    cpt_code: str = ""
    staff_name: str = ""
    pos: str = ""

@dataclass(frozen=True)
class ClientPayment:
    client: str
    payment_date: str
    payment_number: str
    invoice_number: str
    total_appointments: int
    expected_total: float
    appointments: tuple[AppointmentPayment, ...]
