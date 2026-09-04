Build a relational database postgres with 5 tables for tracking at risk payments
and their automated recovery

Table: cases - one row per at-risk revenue event
id(string, key) - Razorpay payment/order ID or generated UUID
customer_id: (string)
amount (float)
source_type (string) - one of:checkout_dropoff, subscription_failed, invoice_overdue
raw_error_code (string, nullable): eg.: CARD_EXPIRED, INSUFFICIENT_FUNDS
root_cause(string, nullable): eg.: card_expired, gateway_timeout
status(string, dafault open): one of: open, recovered, escalated, closed_lost
created_at(datetime)

table: actions - one row per action taken on a case
id (int, PK, autoincrement)
case_id (string, FK->case.id)
action_type (string) - one of: regenerate_link, send_reminder, escalate_to_human, no_action
channel(string, nullable) - email, sms, whatsapp
message(string, nullable) - the drafted text sent to the customer
agent_reasoning (string, nullable) - the LLM's stated reasoning before acting
created_at (datetime)

Table: promises - customer payment commitments
id (int, PK, autoinc)
case_id (string, FK -> case.id)
promised_date (datetime)
status (string, default pending) - pending, kept, broken
created_at(datetime)

Table: audit_log - full timelime of every event on a case
id (int, PK, autoinc)
case_id (string FK -> cases.id)
event (string) - free-text description, eg. "Payment failed -> root_cause=card_expired"
created_at(datetime)


Table: human_review_queue - escalted cases awaiting human follow-up
id(int, PK, autoinc.)
case_id (string, FK-cases.id)
reason (string) - caps_exceeded, opt_out, broken_promise
reviewed (boolean, default false)
escalated_at(datetime)

Relationships: cases is the parent table; actions, promises, audit_log, and human_review_queue all reference cases.id (one case can have many rows in each).
