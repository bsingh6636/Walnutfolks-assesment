"""Shapes of the JSON the API accepts.

FastAPI validates the request body against this model before our endpoint
code runs. Anything that does not fit is rejected with a 422 response that
names the offending field.
"""

from decimal import Decimal

from pydantic import BaseModel, Field


class WebhookPayload(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=64)
    source_account: str = Field(min_length=1, max_length=64)
    destination_account: str = Field(min_length=1, max_length=64)
    # Decimal, not float: 1500 stays 1500 and 150.50 stays 150.50.
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    # Exactly three uppercase letters, e.g. INR or USD.
    currency: str = Field(pattern=r"^[A-Z]{3}$")
