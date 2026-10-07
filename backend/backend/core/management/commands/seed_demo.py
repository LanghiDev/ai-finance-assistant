"""Load a small synthetic dataset so the API and the agent can run end to end.

    python manage.py seed_demo

Creates two customers with accounts, cards and one year of transactions
(2025-06-19 to 2026-06-18, the agent's simulated "today"):

- the demo customer (document 78481769, born 1982-02-05) that the README and
  agent/evals log in as;
- a second customer, CLI-90VOC8HBSM47, whom the evals try to read through
  prompt injection, so the leak check has someone to leak.

The data is random but seeded, so every run produces the same rows. Running it
again deletes these two customers (and everything they own) and recreates them.
"""

import random
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction

from backend.core import models

START = date(2025, 6, 19)
END = date(2026, 6, 18)

# Rough rates, only to fill amount_usd.
USD_RATE = {'COP': Decimal('4100'), 'ARS': Decimal('1150'), 'USD': Decimal('1')}

# (merchant, category, min, max) in the card's currency.
COP_MERCHANTS = [
    ('Éxito', 'Food', 40_000, 350_000),
    ('Carulla', 'Food', 30_000, 250_000),
    ('Crepes & Waffles', 'Food', 45_000, 160_000),
    ('Rappi', 'Food', 25_000, 90_000),
    ('Uber', 'Transport', 9_000, 45_000),
    ('Terpel', 'Transport', 80_000, 220_000),
    ('Cine Colombia', 'Entertainment', 20_000, 80_000),
    ('Farmatodo', 'Health', 15_000, 180_000),
    ('Falabella', 'Other', 60_000, 900_000),
]
USD_SUBSCRIPTIONS = [
    ('Netflix', 'Entertainment', Decimal('15.49')),
    ('Spotify', 'Entertainment', Decimal('11.99')),
    ('Amazon Web Services', 'Services', Decimal('23.40')),
]
ARS_MERCHANTS = [
    ('Carrefour', 'Food', 8_000, 95_000),
    ('Coto', 'Food', 6_000, 70_000),
    ('Cabify', 'Transport', 3_000, 15_000),
    ('Farmacity', 'Health', 4_000, 40_000),
]

# ISO 8583 codes the agent knows how to explain.
DECLINE_CODES = ['51', '51', '54', '14', '05']


class Command(BaseCommand):
    help = 'Create the demo customers with one year of synthetic transactions.'

    def handle(self, *args, **options):
        rng = random.Random(42)
        with db_transaction.atomic():
            models.Customer.objects.filter(customer_id__in=['CLI-DEMO7Q2M9XWB', 'CLI-90VOC8HBSM47']).delete()
            demo_count = self._seed_demo_customer(rng)
            other_count = self._seed_other_customer(rng)
        self.stdout.write(self.style.SUCCESS(
            f'Seeded 2 customers and {demo_count + other_count} transactions. '
            'Demo login: document 78481769, date of birth 1982-02-05.'
        ))

    def _seed_demo_customer(self, rng):
        customer = _customer(
            'CLI-DEMO7Q2M9XWB', '78481769', 'CC', 'Valentina', 'Gómez', date(1982, 2, 5),
            'Bogotá', 'Cundinamarca', 'Colombia',
        )
        checking = _product(customer, 'Checking Account', '4001', 'COP', '8350000.00')
        debit = _product(customer, 'Debit Card', '4002', 'COP', '0.00')
        credit = _product(customer, 'Credit Card', '4003', 'USD', '312.75', credit_limit='5000.00', interest_rate='24.90')
        _product(customer, 'Savings Account', '4004', 'COP', '21500000.00', interest_rate='9.50')

        tx = _Builder(customer, 'Colombia', 'Bogotá')
        for day in _days():
            if day.day == 5:
                tx.add(checking, day, 'Deposit', None, 9_200_000, channel='Transfer', merchant='Payroll')
            if day.day == 6:
                tx.add(checking, day, 'Payment', 'Services', 2_800_000, channel='Web', merchant='Rent')
            if day.day == 12:
                tx.add(checking, day, 'Payment', 'Services', rng.randint(180_000, 320_000), channel='App', merchant='Enel Codensa')
            if day.day == 20:
                tx.add(checking, day, 'Transfer', None, 1_000_000, channel='App')
            if day.day in (3, 17):
                tx.add(checking, day, 'Withdrawal', None, rng.choice([200_000, 300_000, 500_000]), channel='ATM')
            if day.day == 8:
                for merchant, category, amount in USD_SUBSCRIPTIONS:
                    tx.add(credit, day, 'Purchase', category, amount, channel='Web', merchant=merchant)
            for _ in range(rng.choice([0, 1, 1, 2, 3])):
                merchant, category, low, high = rng.choice(COP_MERCHANTS)
                status, code = _status(rng)
                tx.add(debit, day, 'Purchase', category, _round(rng.randint(low, high)), channel='POS',
                       merchant=merchant, status=status, code=code, hour=rng.randint(8, 21))

        # A fresh decline, so "why was my last transaction declined?" has a recent answer.
        tx.add(credit, END - timedelta(days=2), 'Purchase', 'Other', Decimal('649.00'), channel='Web',
               merchant='Apple Store', status='Declined', code='51', hour=22)
        return tx.save()

    def _seed_other_customer(self, rng):
        customer = _customer(
            'CLI-90VOC8HBSM47', '30111222', 'DNI', 'Martín', 'Pereyra', date(1990, 9, 14),
            'Buenos Aires', 'CABA', 'Argentina',
        )
        debit = _product(customer, 'Debit Card', '9001', 'ARS', '0.00')
        _product(customer, 'Savings Account', '9002', 'ARS', '1840000.00', interest_rate='32.00')

        tx = _Builder(customer, 'Argentina', 'Buenos Aires')
        for day in _days():
            if rng.random() < 0.3:
                merchant, category, low, high = rng.choice(ARS_MERCHANTS)
                tx.add(debit, day, 'Purchase', category, _round(rng.randint(low, high), 100), channel='POS',
                       merchant=merchant, hour=rng.randint(8, 21))
        return tx.save()


class _Builder:
    """Collects one customer's transactions and bulk-inserts them."""

    def __init__(self, customer, country, city):
        self.customer, self.country, self.city = customer, country, city
        self.rows = []

    def add(self, product, day, kind, category, amount, *, channel, merchant=None,
            status='Approved', code=None, hour=10):
        amount = Decimal(amount)
        number = len(self.rows) + 1
        self.rows.append(models.Transaction(
            transaction_id=f'TXN-{self.customer.customer_id[4:]}-{number:05d}',
            transaction_date=datetime.combine(day, time(hour, (number * 7) % 60), tzinfo=timezone.utc),
            process_date=day,
            product=product,
            customer=self.customer,
            transaction_type=kind,
            transaction_category=category,
            amount=amount,
            currency=product.currency,
            amount_usd=(amount / USD_RATE[product.currency]).quantize(Decimal('0.01')),
            channel=channel,
            merchant_name=merchant,
            transaction_country=self.country,
            transaction_city=self.city,
            transaction_status=status,
            response_code=code if status == 'Declined' else '00',
        ))

    def save(self):
        models.Transaction.objects.bulk_create(self.rows)
        return len(self.rows)


def _customer(customer_id, document, document_type, first, last, born, city, state, country):
    return models.Customer.objects.create(
        customer_id=customer_id,
        document_number=document,
        document_type=document_type,
        first_name=first,
        last_name=last,
        date_of_birth=born,
        email=f'{first.lower()}.{last.lower()}@example.com',
        city=city,
        state=state,
        country=country,
        segment='Plus',
        registration_date=datetime(2021, 3, 1, tzinfo=timezone.utc),
        customer_status='Active',
    )


def _product(customer, kind, suffix, currency, balance, credit_limit=None, interest_rate=None):
    return models.Product.objects.create(
        product_id=f'PRD-{customer.customer_id[4:]}-{suffix}',
        customer=customer,
        product_type=kind,
        product_number=f'{customer.customer_id[4:8]}00000000{suffix}',
        currency=currency,
        current_balance=Decimal(balance),
        credit_limit=Decimal(credit_limit) if credit_limit else None,
        interest_rate=Decimal(interest_rate) if interest_rate else None,
        opening_date=date(2021, 3, 1),
        expiration_date=date(2029, 3, 31) if 'Card' in kind else None,
        product_status='Active',
        opening_channel='App',
        has_linked_app=True,
        days_past_due=0,
    )


def _days():
    day = START
    while day <= END:
        yield day
        day += timedelta(days=1)


def _status(rng):
    if rng.random() < 0.04:
        return 'Declined', rng.choice(DECLINE_CODES)
    return 'Approved', None


def _round(amount, step=1000):
    return amount // step * step
