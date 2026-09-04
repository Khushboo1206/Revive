{
    "entity": "event",
    "account_id": "acc_TU5MM66t2Db73M",
    "event": "payment.failed",
    "contains": [
        "payment"
    ],
    "payload": {
        "payment": {
            "entity": {
                "id": "pay_TVC1BZmIY5LSPS",
                "entity": "payment",
                "amount": 976700,
                "currency": "INR",
                "status": "failed",
                "order_id": "order_TVBeUILXxVjyXG",
                "invoice_id": null,
                "international": false,
                "method": "card",
                "amount_refunded": 0,
                "refund_status": null,
                "captured": false,
                "description": null,
                "card_id": "card_TVC1BlMY0vhcsy",
                "card": {
                    "id": "card_TVC1BlMY0vhcsy",
                    "entity": "card",
                    "name": "",
                    "last4": "0005",
                    "network": "MasterCard",
                    "type": "credit",
                    "issuer": "UTIB",
                    "international": false,
                    "emi": true,
                    "sub_type": "consumer",
                    "token_iin": "530562000"
                },
                "bank": null,
                "wallet": null,
                "vpa": null,
                "email": "123@email.com",
                "contact": "+919767190758",
                "notes": {
                    "email": "123@email.com",
                    "phone": "+919767190758"
                },
                "fee": null,
                "tax": null,
                "error_code": "BAD_REQUEST_ERROR",
                "error_description": "Your payment could not be completed due to a temporary issue. Try again later.",
                "error_source": "gateway",
                "error_step": "payment_authorization",
                "error_reason": "payment_timed_out",
                "acquirer_data": {
                    "auth_code": null
                },
                "created_at": 1787920780,
                "reward": null
            }
        }
    },
    "created_at": 1787920788
}