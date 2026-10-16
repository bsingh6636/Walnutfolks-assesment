# Transaction Webhook Service

A small FastAPI service that receives transaction webhooks from a payment
processor, acknowledges them immediately, and processes each transaction
reliably in the background.

## Endpoints

| Method | Path                                  | Purpose                              |
| ------ | ------------------------------------- | ------------------------------------ |
| GET    | `/`                                   | Health check                         |
| POST   | `/v1/webhooks/transactions`           | Receive a webhook, returns 202       |
| GET    | `/v1/transactions/{transaction_id}`   | Look up a transaction and its status |

## Running locally

_To be filled in._

## Running the tests

_To be filled in._

## Technical choices

_To be filled in._

## Deployment

_To be filled in._
