#!/usr/bin/env python3
"""Отчёт «аванс по договору»: сколько выплачено и сколько осталось по каждой работе и по договору.

Использование:
    python3 report.py [--date YYYY-MM-DD] [--data DIR] [--csv FILE]

Деньги считаются в Decimal, без float. По умолчанию дата отчёта - 2026-11-10,
данные читаются из ./data.
"""

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

ZERO = Decimal("0")
CENT = Decimal("0.01")


@dataclass
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date


@dataclass
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str
    completed_at: date | None
    cancelled_at: date | None
    paid: Decimal = ZERO


@dataclass
class Payment:
    payment_id: str
    contract_id: str
    work_id: str | None
    purpose: str
    amount: Decimal
    status: str
    created_at: date
    paid_at: date | None
    refunded_at: date | None

    def net_paid_at(self, report_date: date) -> Decimal:
        """Сколько денег по платежу фактически «у подрядчика» на дату отчёта."""
        net = ZERO
        if self.paid_at and self.paid_at <= report_date:
            net += self.amount
        if self.refunded_at and self.refunded_at <= report_date:
            net -= self.amount
        return net

    def in_queue_at(self, report_date: date) -> bool:
        return self.status == "queued" and self.created_at <= report_date


def parse_date(s: str) -> date | None:
    s = (s or "").strip()
    return date.fromisoformat(s) if s else None


def parse_money(s: str) -> Decimal:
    return Decimal(s.strip())


def load_data(data_dir: Path):
    contracts, works, payments = {}, {}, []
    with open(data_dir / "contracts.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            contracts[row["contract_id"]] = Contract(
                contract_id=row["contract_id"],
                contractor=row["contractor"],
                amount=parse_money(row["amount"]),
                signed_at=date.fromisoformat(row["signed_at"]),
            )
    with open(data_dir / "works.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            works[row["work_id"]] = Work(
                work_id=row["work_id"],
                contract_id=row["contract_id"],
                title=row["title"],
                price=parse_money(row["price"]),
                status=row["status"],
                completed_at=parse_date(row.get("completed_at")),
                cancelled_at=parse_date(row.get("cancelled_at")),
            )
    with open(data_dir / "payments.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            payments.append(Payment(
                payment_id=row["payment_id"],
                contract_id=row["contract_id"],
                work_id=row["work_id"].strip() or None,
                purpose=row["purpose"],
                amount=parse_money(row["amount"]),
                status=row["status"],
                created_at=date.fromisoformat(row["created_at"]),
                paid_at=parse_date(row.get("paid_at")),
                refunded_at=parse_date(row.get("refunded_at")),
            ))
    return contracts, works, payments


def work_order_key(w: Work):
    """Порядок, в котором договорные деньги покрывают работы:
    выполненные - по дате приёмки, затем запланированные по номеру."""
    if w.status == "done":
        return (0, w.completed_at or date.min, w.work_id)
    return (1, date.min, w.work_id)


def allocate(works: dict[str, Work], payments: list[Payment], report_date: date):
    """Разносит платежи по работам.

    Платежи с work_id - напрямую на свою работу. Платежи без привязки
    распределяются «водопадом»: покрывают работы по порядку, пока хватает
    остатка цены. Излишек сверх всех работ не распределяется.
    """
    active_payments = []
    for p in payments:
        net = p.net_paid_at(report_date)
        if net > ZERO:
            active_payments.append((p, net))
    active_payments.sort(key=lambda t: (t[0].paid_at or t[0].created_at, t[0].payment_id))

    unallocated: dict[str, Decimal] = {}
    for contract_id in {w.contract_id for w in works.values()}:
        ordered = sorted(
            (w for w in works.values()
             if w.contract_id == contract_id and w.status != "cancelled"),
            key=work_order_key,
        )
        idx = 0
        for p, amount in active_payments:
            if p.contract_id != contract_id:
                continue
            if p.work_id:
                if p.work_id in works:
                    works[p.work_id].paid += amount
                else:
                    unallocated[contract_id] = unallocated.get(contract_id, ZERO) + amount
                continue
            # платёж по договору без привязки - «водопад» по работам
            remaining = amount
            while remaining > ZERO and idx < len(ordered):
                w = ordered[idx]
                capacity = w.price - w.paid
                if capacity <= ZERO:
                    idx += 1
                    continue
                take = min(capacity, remaining)
                w.paid += take
                remaining -= take
                if w.paid >= w.price:
                    idx += 1
        # деньги, не покрывшие ни одну работу, - переплата/нераспределённый аванс
        # (учитывается в платеже ниже через сумму net платежей договора)

    return unallocated


def fmt_money(d: Decimal) -> str:
    q = d.quantize(CENT)
    s = f"{abs(q):,.2f}".replace(",", " ").replace(".", ",")
    return ("-" if q < 0 else "") + s


def fmt_signed(d: Decimal) -> str:
    return ("+" if d > 0 else "") + fmt_money(d)


def build_report(contracts, works, payments, report_date):
    allocate(works, payments, report_date)

    # платежи, реально ушедшие подрядчику на дату (нетто)
    paid_by_contract = {}
    queued_by_contract = {}
    for p in payments:
        net = p.net_paid_at(report_date)
        paid_by_contract[p.contract_id] = paid_by_contract.get(p.contract_id, ZERO) + net
        if p.in_queue_at(report_date):
            queued_by_contract[p.contract_id] = queued_by_contract.get(p.contract_id, ZERO) + p.amount

    sections = []
    for cid in sorted(contracts):
        c = contracts[cid]
        cworks = [works[wid] for wid in works if works[wid].contract_id == cid]
        cworks.sort(key=lambda w: work_order_key(w) if w.status != "cancelled" else (2, w.work_id))
        sections.append((c, cworks))
    return sections, paid_by_contract, queued_by_contract


def print_report(sections, paid_by_contract, queued_by_contract, report_date):
    print(f"Отчёт по авансам и оплатам на {report_date.isoformat()}\n")

    for c, cworks in sections:
        print(f"=== {c.contract_id} — {c.contractor} ===")
        print(f"Договор: {fmt_money(c.amount)} руб., подписан {c.signed_at.isoformat()}")
        print()
        hdr = f"{'Работа':<34}{'Цена':>14}{'Выплачено':>14}{'Осталось':>14}  Статус"
        print(hdr)
        print("-" * len(hdr))
        total_price = ZERO
        total_paid_attr = ZERO
        total_remaining_pos = ZERO
        total_overpaid = ZERO
        for w in cworks:
            if w.status == "cancelled":
                remaining = ZERO
            else:
                remaining = w.price - w.paid
            label = f"{w.work_id} {w.title}"
            print(f"{label:<34}{fmt_money(w.price):>14}{fmt_money(w.paid):>14}"
                  f"{fmt_money(remaining):>14}  {w.status}")
            if w.status != "cancelled":
                total_price += w.price
                total_paid_attr += w.paid
                if remaining > ZERO:
                    total_remaining_pos += remaining
                elif remaining < ZERO:
                    total_overpaid += -remaining
        paid_total = paid_by_contract.get(c.contract_id, ZERO)
        unallocated = paid_total - total_paid_attr
        print("-" * len(hdr))
        print(f"{'ИТОГО по договору':<34}{fmt_money(total_price):>14}{fmt_money(paid_total):>14}"
              f"{fmt_money(total_remaining_pos):>14}")
        print(f"  распределено по работам: {fmt_money(total_paid_attr)} руб.; "
              f"не распределено (сверх работ): {fmt_money(unallocated)} руб.")
        if total_overpaid > ZERO:
            print(f"  переплата по работам (выплачено сверх цены): {fmt_money(total_overpaid)} руб.")
        queued = queued_by_contract.get(c.contract_id, ZERO)
        if queued > ZERO:
            print(f"  в очереди на оплату (не выплачено): {fmt_money(queued)} руб.")
        print()


def write_csv(sections, paid_by_contract, queued_by_contract, report_date, path: Path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(["contract_id", "contractor", "work_id", "work", "price",
                     "paid", "remaining", "status", "report_date"])
        for c, cworks in sections:
            for w in cworks:
                remaining = ZERO if w.status == "cancelled" else w.price - w.paid
                wr.writerow([c.contract_id, c.contractor, w.work_id, w.title,
                             f"{w.price:.2f}", f"{w.paid:.2f}", f"{remaining:.2f}",
                             w.status, report_date.isoformat()])
        for c, cworks in sections:
            non_cancelled = [w for w in cworks if w.status != "cancelled"]
            price_total = sum((w.price for w in non_cancelled), ZERO)
            paid_total = paid_by_contract.get(c.contract_id, ZERO)
            remaining_total = sum(
                (max(ZERO, w.price - w.paid) for w in non_cancelled), ZERO)
            wr.writerow([c.contract_id, c.contractor, "", "ИТОГО", f"{price_total:.2f}",
                         f"{paid_total:.2f}", f"{remaining_total:.2f}", "", report_date.isoformat()])


def main():
    ap = argparse.ArgumentParser(description="Отчёт по оплатам договоров на дату.")
    ap.add_argument("--date", default="2026-11-10",
                    help="Дата отчёта YYYY-MM-DD (по умолчанию 2026-11-10)")
    ap.add_argument("--data", default="data", help="Папка с CSV (по умолчанию ./data)")
    ap.add_argument("--csv", metavar="FILE", help="Дополнительно записать отчёт в CSV")
    args = ap.parse_args()

    report_date = date.fromisoformat(args.date)
    data_dir = Path(args.data)
    contracts, works, payments = load_data(data_dir)

    # allocate() мутирует Work.paid - грузим данные заново не нужно,
    # но для чистоты работаем на свежих объектах
    sections, paid_by_contract, queued_by_contract = build_report(
        contracts, works, payments, report_date)

    print_report(sections, paid_by_contract, queued_by_contract, report_date)
    if args.csv:
        write_csv(sections, paid_by_contract, queued_by_contract, report_date, Path(args.csv))
        print(f"CSV записан: {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
